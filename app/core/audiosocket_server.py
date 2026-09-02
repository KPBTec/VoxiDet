"""
core/audiosocket_server.py — Servidor AudioSocket (v1.27.0), modo alternativo
al stream por WebSocket de app/api/stream.py.

Por qué existe esto: AudioSocket() es una aplicación NATIVA del dialplan de
Asterisk (no un script AGI) — Asterisk mismo bombea el audio del canal hacia
y desde el socket TCP, sin que ningún proceso Python tenga que leerlo de un
file descriptor y reenviarlo a mano (como sí hace el modo Stream actual vía
EAGI + WebSocket armado en `app/core/agi_template.py`). Dos ventajas reales
sobre EAGI, no solo "otra forma de hacer lo mismo":

1. Bidireccional de fábrica — se le puede mandar audio de confort (silencio
   sintético) de vuelta al que llama MIENTRAS se analiza, evitando el
   problema real reportado en producción de llamadas "mudas" (EAGI es
   unidireccional: fd 3 solo lee, no hay forma de escribir audio de vuelta
   sin usar el protocolo AGI síncrono normal, que bloquearía la lectura).
2. Menos protocolo hecho a mano en el AGI — antes había que armar el
   handshake WebSocket + framing a pulso en stdlib puro (fuente real de bugs
   de producción: la key de Sec-WebSocket-Key mal codificada, el timeout de
   auth silencioso). Acá Asterisk se encarga de todo el transporte de audio.

Modelo de concurrencia — a propósito NO como el de referencia (un script de
un tercero comparado en la sesión que dio origen a este archivo): ese script
llama al decode de Vosk (trabajo de CPU) DIRECTO adentro de una corutina
async, bloqueando el event loop entero mientras decodifica — con varias
llamadas simultáneas, cada decode frena la escritura de silencio de TODAS
las demás conexiones activas al mismo tiempo. Acá se reusa el mismo patrón
ya usado en local_asr.py (run_in_executor + semáforo) para que el trabajo de
CPU nunca bloquee el loop que atiende las demás conexiones.

Autenticación: AudioSocket solo manda un UUID por conexión, no headers ni
query params — no hay forma de mandar la API key ahí. Por eso el AGI hace
una llamada HTTP corta de "registro" (POST /amd/audiosocket/register, mismo
patrón que /amd/check) ANTES de que el dialplan invoque AudioSocket(), dejando
en Redis qué cliente corresponde a ese UUID. Cuando la conexión llega, este
servidor busca el cliente por el UUID — si no lo encuentra (nunca se
registró, o expiró), corta la conexión de una.

Resultado: se guarda en Redis (no en memoria del proceso, a diferencia del
script de referencia) porque acá hay 11 workers — el resultado tiene que
poder leerse desde cualquiera de ellos, no solo el que atendió la conexión
AudioSocket. El AGI lo consulta después con GET /amd/audiosocket/result,
mismo patrón que el `vosk_result_reader.py` de referencia pero por HTTP en
vez de un socket Unix local, porque VoxiDet es centralizado (un servidor
atendiendo Asterisk de clientes distintos por internet), no todo en la
misma máquina.

Bind del socket: se crea ANTES del fork de gunicorn (llamar
create_listening_socket() a nivel de módulo en main.py, igual que los
modelos locales) para que los 11 workers compartan el mismo socket ya
escuchando — mismo mecanismo que ya usa gunicorn para su propio puerto
HTTP, en vez de que cada worker intente bindear el puerto por su cuenta
(fallaría con "Address already in use" en todos menos el primero).
"""
import asyncio
import json
import logging
import socket
import struct
import time
import uuid as uuid_mod

from app.config import settings

log = logging.getLogger("voxidet.audiosocket")

PKT_HANGUP = 0x00
PKT_UUID   = 0x01
PKT_AUDIO  = 0x10

MAX_SECS       = 8.0   # mismo límite duro que el modo stream por WebSocket
SILENCE_FRAME  = bytes([PKT_AUDIO, 0x01, 0x40]) + b"\x00" * 320  # 320B = 20ms slin16 @ 8kHz
SILENCE_PERIOD = 0.02

_PENDING_TTL = 30   # segundos que el registro previo espera a que llegue la conexión AudioSocket
_RESULT_TTL  = 60   # segundos que el resultado espera a que el AGI lo consulte


def create_listening_socket() -> socket.socket:
    """Bind+listen ANTES del fork — llamar a nivel de módulo en main.py,
    igual que init_vosk()/init_sherpa(). El fd se hereda por copy-on-write,
    así que start_audiosocket_server() en cada worker solo tiene que
    envolverlo con asyncio, nunca bindear de nuevo."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", settings.AUDIOSOCKET_PORT))
    sock.listen(256)
    sock.setblocking(False)
    log.info("AudioSocket: socket bindeado en 0.0.0.0:%d (pre-fork)", settings.AUDIOSOCKET_PORT)
    return sock


def _parse_uuid(payload: bytes) -> str | None:
    if len(payload) != 16:
        return None
    return str(uuid_mod.UUID(bytes=payload))


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int | None, bytes]:
    try:
        header = await reader.readexactly(3)
    except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
        return None, b""
    pkt_type = header[0]
    pkt_len  = struct.unpack(">H", header[1:3])[0]
    if pkt_len == 0:
        return pkt_type, b""
    try:
        payload = await reader.readexactly(pkt_len)
    except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
        return None, b""
    return pkt_type, payload


async def _silence_loop(writer: asyncio.StreamWriter, stop: asyncio.Event) -> None:
    """Audio de confort mientras se analiza — a diferencia del modo Stream
    actual (EAGI, unidireccional), acá SÍ se puede mandar algo de vuelta.
    Sin esto, quien llama escucha silencio muerto en vez de línea viva
    mientras dura la detección (reportado en producción como llamadas
    "mudas" — ver CHANGELOG v1.27.0)."""
    try:
        while not stop.is_set():
            writer.write(SILENCE_FRAME)
            await writer.drain()
            await asyncio.sleep(SILENCE_PERIOD)
    except Exception:
        pass


async def _resolve_pending(call_uuid: str) -> dict | None:
    """Busca y consume (one-shot) el registro previo hecho por
    POST /amd/audiosocket/register. None si nunca se registró o ya expiró
    — la conexión AudioSocket se corta de una en ese caso, no hay forma de
    autenticar sin ese registro (ver docstring del módulo)."""
    try:
        from app.cache.client_cache import get_redis
        r = await get_redis()
        key = f"audiosocket:pending:{call_uuid}"
        raw = await r.get(key)
        if not raw:
            return None
        await r.delete(key)
        return json.loads(raw)
    except Exception as e:
        log.warning("AudioSocket: error resolviendo pending uuid=%s: %s", call_uuid, e)
        return None


async def _store_result(call_uuid: str, payload: dict) -> None:
    try:
        from app.cache.client_cache import get_redis
        r = await get_redis()
        await r.setex(f"audiosocket:result:{call_uuid}", _RESULT_TTL, json.dumps(payload))
    except Exception as e:
        log.warning("AudioSocket: error guardando resultado uuid=%s: %s", call_uuid, e)


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    # Los imports pesados (numpy vía silero_vad, DB, etc.) van DESPUÉS de
    # confirmar que la conexión tiene un registro válido — antes vivían acá
    # arriba y corrían en TODAS las conexiones, incluidas las que se iban a
    # rechazar de una por no tener /register previo (una fuente de tráfico
    # basura/mal configurado no debería pagar el costo de cargar el motor de
    # detección solo para ser rechazada).
    peer = writer.get_extra_info("peername")
    call_uuid: str | None = None
    t0 = time.monotonic()

    pkt_type, payload = await _read_packet(reader)
    if pkt_type != PKT_UUID:
        log.warning("AudioSocket: primer paquete no es UUID (tipo=%s) desde %s", pkt_type, peer)
        writer.close()
        return
    call_uuid = _parse_uuid(payload)
    if not call_uuid:
        log.warning("AudioSocket: UUID inválido desde %s", peer)
        writer.close()
        return

    pending = await _resolve_pending(call_uuid)
    if not pending:
        log.warning("AudioSocket: uuid=%s sin registro previo (falta el paso de /register en el dialplan, o expiró)", call_uuid)
        try:
            writer.write(bytes([PKT_HANGUP, 0x00, 0x00]))
            await writer.drain()
        except Exception:
            pass
        writer.close()
        return

    from app.core.silero_vad import make_detector
    from app.db.providers import get_vad_engine
    from app.api.stream import _decide_and_send
    from app.db.logs import save_log
    from app.db.client_keywords import get_cached_client_keywords

    client      = pending["client"]
    provider    = client.get("provider", "groq")
    session_id  = f"as-{call_uuid[:8]}"
    log.info("[%s] AudioSocket conectado uuid=%s cliente=%s provider=%s", session_id, call_uuid, client["name"], provider)

    keywords_mode = client.get("keywords_mode", "global")
    ckw_human, ckw_voicemail = set(), set()
    if keywords_mode == "custom":
        try:
            ckw_human, ckw_voicemail = await get_cached_client_keywords(client["id"])
        except Exception:
            pass

    stop_silence = asyncio.Event()
    sil_task = asyncio.create_task(_silence_loop(writer, stop_silence))

    vad_engine = await get_vad_engine()
    detector = make_detector(vad_engine)

    result, layer, transcript = "UNKNOWN", 1, ""
    audio_bytes = 0

    async def _do_decide() -> tuple[str, int, str]:
        return await _decide_and_send(
            None, detector, session_id, t0, provider,
            extra_human=ckw_human or None,
            extra_voicemail=ckw_voicemail or None,
            aggressive=client.get("amd_bias") == "aggressive",
        )

    try:
        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= MAX_SECS:
                result, layer, transcript = await _do_decide()
                break

            pkt_type, chunk = await _read_packet(reader)

            if pkt_type is None:
                log.info("[%s] EOF/desconexión audio=%dB", session_id, audio_bytes)
                if audio_bytes > 0:
                    result, layer, transcript = await _do_decide()
                break

            if pkt_type == PKT_HANGUP:
                log.info("[%s] hangup packet audio=%dB", session_id, audio_bytes)
                if audio_bytes > 0:
                    result, layer, transcript = await _do_decide()
                break

            if pkt_type != PKT_AUDIO or not chunk:
                continue

            audio_bytes += len(chunk)
            decision = detector.feed(chunk)
            if decision:
                result, layer, transcript = await _do_decide()
                break

    except Exception as e:
        log.error("[%s] error: %s", session_id, e)
    finally:
        stop_silence.set()
        sil_task.cancel()
        try:
            writer.write(bytes([PKT_HANGUP, 0x00, 0x00]))
            await writer.drain()
        except Exception:
            pass
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:
            pass

    latency_ms = int((time.monotonic() - t0) * 1000)
    _transcript = transcript[:200] if transcript else ("[sin audio]" if audio_bytes == 0 else "[silencio]")

    await _store_result(call_uuid, {
        "status":      result,
        "layer_used":  layer,
        "latency_ms":  latency_ms,
        "transcript":  _transcript,
    })

    _t = asyncio.create_task(save_log(
        client_id     = client["id"],
        call_id       = pending.get("call_id", "")[:100],
        caller_id     = pending.get("caller_id", "")[:50],
        result        = result,
        layer         = layer,
        mode          = "audiosocket",
        latency_ms    = latency_ms,
        audio_secs    = round(audio_bytes / (8000 * 2), 2),
        provider      = provider if layer == 2 else "",
        transcript    = _transcript,
        param1        = pending.get("lead_id", ""),
        param2        = session_id,
        param4        = (f"{pending.get('campaign_id','')}|{pending.get('list_id','')}"
                         if pending.get("campaign_id") else pending.get("list_id", "")),
        beep_detected = detector.tone_detected(),
    ))
    from app.api.amd import _log_task_exception
    _t.add_done_callback(_log_task_exception)

    log.info("[%s] → %s layer=%d %dms transcript='%s'", session_id, result, layer, latency_ms, _transcript)


async def start_audiosocket_server(sock: socket.socket) -> asyncio.base_events.Server:
    server = await asyncio.start_server(handle_connection, sock=sock)
    log.info("AudioSocket: worker escuchando (socket heredado, pid=%d)", __import__("os").getpid())
    return server
