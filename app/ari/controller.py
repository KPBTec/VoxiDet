"""
app/ari/controller.py — Controlador ARI para el modo Audiosocket/RTP (EXPERIMENTAL, v1.28.0).

## Por qué existe

En Asterisk 18.26.4 (nodo de producción real) la app de dialplan
`AudioSocket()` SIEMPRE "sale con error" — confirmado leyendo el código
fuente real de esa versión (`apps/app_audiosocket.c::audiosocket_run`):
llama a `ast_audiosocket_receive_frame()` (la variante SIN manejo de
hangup), así que cualquier cierre de la conexión — prolijo o no, nuestro o
del que llama colgando — se interpreta como error y el dialplan NUNCA
continúa a la siguiente prioridad. Encontrado en producción real (v1.27.5 a
v1.27.7): con ese modelo, un HUMAN detectado correctamente igual termina
colgado en vez de transferido al agente, porque la línea que sigue después
de `AudioSocket()` nunca se ejecuta.

ARI (la API REST + WebSocket de eventos de Asterisk) no tiene ese problema:
en vez de que el dialplan dependa de que una aplicación "termine bien", un
servicio aparte toma el control del canal real vía `Stasis()`, hace lo que
tenga que hacer, y cuando termina LE DICE explícitamente al canal que
continúe el dialplan (`POST /channels/{id}/continue`) — nunca depende de un
código de salida.

## Dos formas de recibir el audio (`settings.ARI_MEDIA_MODE`)

Encontrado en producción: no todos los nodos Asterisk tienen
`chan_audiosocket` instalado (confirmado real en un nodo con Asterisk
16.30.0-vici, `module load chan_audiosocket.so` → "Unable to load module").
Por eso hay dos caminos, elegidos por `settings.ARI_MEDIA_MODE`:

- **"audiosocket"** (para nodos que SÍ tienen `chan_audiosocket`): crea un
  canal de esa tecnología vía ARI, apuntando al MISMO servidor TCP que ya
  corre siempre para el modo Audiosocket clásico
  (`app/core/audiosocket_server.py`, sin ningún cambio ahí) — solo cambia
  quién maneja el ciclo de vida del canal (ARI, no el dialplan).
- **"rtp"** (para nodos sin ese módulo, ej. el de 16.30.0-vici real): usa
  `externalMedia`, el mecanismo NATIVO de ARI para esto — Asterisk manda el
  audio como RTP crudo por UDP a un puerto nuestro (ver
  `app/ari/rtp_media.py`). No necesita ningún módulo extra en Asterisk,
  pero corre la detección EN ESTE MISMO PROCESO (no delega al servidor
  TCP), así que este proceso también carga los modelos locales de ASR
  (Vosk/Sherpa/Silero) — ver `_load_models()` más abajo.

En ambos casos, el flujo alrededor es el mismo:

1. El dialplan de prueba (extensión 8379, NUNCA la extensión real de un
   cliente — ver CLAUDE.md/README) hace `Stasis(voxidet-ari)` en vez de
   `AudioSocket(...)`. Esto dispara un evento `StasisStart` acá.
2. Lee `VOXIDET_API_KEY` (variable de canal seteada en el dialplan antes de
   `Stasis()`) para identificar el cliente — reusa `get_client_cached()`
   directo (sin round-trip HTTP: este proceso corre server-side, con acceso
   directo a Redis/DB).
3. Crea un canal Snoop sobre el canal real (copia de su audio, sin tocarlo)
   + un canal/sesión de audio (AudioSocket o RTP según el modo) y los
   bridgea/conecta entre sí.
4. Cuando la detección termina (en el servidor TCP para el modo
   "audiosocket", o en este mismo proceso para "rtp"), limpia los canales
   temporales, setea `AMDSTATUS`/`AMDLAYER`/`AMDMS` en el canal real, y lo
   manda a continuar el dialplan en la etiqueta `after-ari`.

## Por qué es un proceso aparte, no otro worker de gunicorn

Mismo hallazgo de HOY, dos veces (ver CHANGELOG v1.27.8 y v1.27.9): un
recurso pensado para "un solo proceso" se rompe si corre multiplicado por
los N workers de gunicorn sin coordinarse entre ellos. Acá sería peor — ARI
mantiene UNA conexión WebSocket persistente por proceso a Asterisk
(`app=voxidet-ari`); si este código corriera dentro de cada uno de los N
workers, Asterisk repartiría (o duplicaría, según versión) los eventos de
Stasis entre esas N conexiones sin ninguna garantía de a cuál le toca cada
llamada. Por eso vive en su propio contenedor/proceso (ver docker-compose.yml,
servicio `ari-controller`) — una sola conexión, sin ambigüedad.

## Estado: NUNCA VALIDADO CONTRA ASTERISK REAL

Todo lo de acá se escribió leyendo la documentación/código de ARI, sin
poder probarlo en vivo desde esta sesión. Puntos concretos sin confirmar:
- Modo "audiosocket": la dirección exacta de `spy` en el Snoop (`spy=in`),
  y si el canal recién originado se puede bridgear antes de estar "Up".
- Modo "rtp": si Asterisk empieza a mandar audio apenas se crea el channel
  externalMedia o hace falta esperar a que esté "Up"; el Payload Type que
  espera ver en los frames de vuelta; reordenamiento de paquetes RTP (no
  implementado, ver rtp_media.py).
Todo esto se prueba contra la extensión 8379 (dialplan de prueba, separado
de cualquier extensión real de producción) antes de siquiera considerar
usarlo con tráfico real.
"""
import asyncio
import json
import logging
import time
import uuid as uuid_mod

import httpx
import websockets

from app.config import settings

log = logging.getLogger("voxidet.ari")

_PENDING_TTL = 30
_RESULT_POLL_INTERVAL = 0.1
_RESULT_POLL_TIMEOUT = 3.0
_MAX_SECS = 8.0   # mismo límite duro que audiosocket_server.py/stream.py


def _load_models() -> None:
    """Solo hace falta para ARI_MEDIA_MODE='rtp' — ese modo corre la
    detección EN ESTE PROCESO (no delega al servidor TCP de
    audiosocket_server.py como sí hace el modo 'audiosocket'), así que
    necesita su propia copia de los modelos locales cargada en memoria.
    Costo real: al ser UN SOLO proceso (no N workers como `api`), es una
    sola copia extra de RAM, no multiplicada — pero sigue siendo una copia
    aparte de la que ya carga el servicio `api` (sin compartir por
    copy-on-write, porque son contenedores/procesos distintos, no un fork
    del mismo padre)."""
    if settings.ARI_MEDIA_MODE != "rtp":
        return
    from app.core.local_asr import init_vosk, init_sherpa, init_sherpa_large, discover_models
    from app.core.silero_vad import init_silero_vad
    models = discover_models(settings.MODELS_BASE)
    if models["vosk"]:
        init_vosk(models["vosk"])
    if models["sherpa"]:
        init_sherpa(models["sherpa"])
    if models["sherpa_large"]:
        init_sherpa_large(models["sherpa_large"])
    if models["silero"]:
        init_silero_vad(models["silero"])
    log.info("ARI (modo rtp): modelos locales cargados (vosk=%s sherpa=%s silero=%s)",
              bool(models["vosk"]), bool(models["sherpa"]), bool(models["silero"]))


def _rest_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{settings.ARI_URL}/ari",
        auth=(settings.ARI_USER, settings.ARI_PASSWORD),
        timeout=10.0,
    )


async def _get_channel_var(
    client: httpx.AsyncClient, channel_id: str, var: str,
    channelvars: dict | None = None,
) -> str:
    """Si Asterisk ya mandó el valor en el propio evento StasisStart (ver
    `channelvars=` en ari.conf y README.md § "Modo ARI") lo usa directo, sin
    round-trip — evita hasta 5 llamadas REST por llamada (una por variable:
    VOXIDET_API_KEY, phone_number, lead_id, campaign_id, list_id). Si esa
    opción no está configurada (channelvars vacío/None), cae a consultarla
    una por una vía REST, más lento pero funciona igual."""
    if channelvars and var in channelvars:
        return channelvars.get(var) or ""
    try:
        r = await client.get(f"/channels/{channel_id}/variable", params={"variable": var})
        if r.status_code == 200:
            return r.json().get("value", "") or ""
    except Exception as e:
        log.warning("ARI: error leyendo variable %s de %s: %s", var, channel_id, e)
    return ""


async def _set_channel_var(client: httpx.AsyncClient, channel_id: str, var: str, value: str) -> None:
    try:
        await client.post(f"/channels/{channel_id}/variable", params={"variable": var, "value": value})
    except Exception as e:
        log.warning("ARI: error seteando variable %s en %s: %s", var, channel_id, e)


async def _hangup_channel(client: httpx.AsyncClient, channel_id: str) -> None:
    try:
        await client.delete(f"/channels/{channel_id}")
    except Exception:
        pass  # ya puede estar colgado/destruido — no es un error real acá


async def _process_call(client: httpx.AsyncClient, channel_id: str, channelvars: dict | None = None) -> None:
    try:
        await _process_call_inner(client, channel_id, channelvars)
    except Exception as e:
        log.error("ARI: error procesando canal %s: %s", channel_id, e, exc_info=e)
        await _hangup_channel(client, channel_id)


async def _process_call_inner(client: httpx.AsyncClient, channel_id: str, channelvars: dict | None = None) -> None:
    from app.cache.client_cache import get_client_cached

    api_key = await _get_channel_var(client, channel_id, "VOXIDET_API_KEY", channelvars)
    if not api_key:
        log.error("ARI: canal %s sin variable VOXIDET_API_KEY (setearla en el dialplan antes de Stasis()) — colgando", channel_id)
        await _hangup_channel(client, channel_id)
        return

    voxidet_client = await get_client_cached(api_key)
    if not voxidet_client or not voxidet_client.get("active"):
        log.warning("ARI: canal %s con api_key inválida o cliente inactivo — colgando", channel_id)
        await _hangup_channel(client, channel_id)
        return

    call_uuid  = str(uuid_mod.uuid4())
    session_id = f"ari-{call_uuid[:8]}"

    # Snoop: copia del audio del canal real sin tocarlo. spy=in ("lo que
    # entra a Asterisk desde ese canal", i.e. la voz de quien llama) — sin
    # confirmar todavía contra un canal real, ver docstring del módulo.
    snoop_id = f"snoop-{call_uuid}"
    resp = await client.post(
        f"/channels/{channel_id}/snoop",
        params={"spy": "in", "app": settings.ARI_APP, "appArgs": "snoop", "snoopId": snoop_id},
    )
    if resp.status_code not in (200, 201):
        log.error("[%s] ARI: no se pudo crear canal Snoop (%s): %s", session_id, resp.status_code, resp.text)
        await _hangup_channel(client, channel_id)
        return

    if settings.ARI_MEDIA_MODE == "rtp":
        result = await _run_rtp_mode(client, session_id, call_uuid, snoop_id, voxidet_client)
    else:
        result = await _run_audiosocket_mode(client, session_id, call_uuid, snoop_id, voxidet_client, channel_id, channelvars)

    if result is None:
        result = {"status": "ERROR", "layer_used": 0, "latency_ms": 0}

    await _set_channel_var(client, channel_id, "AMDSTATUS", result.get("status", "ERROR"))
    await _set_channel_var(client, channel_id, "AMDLAYER",  str(result.get("layer_used", 0)))
    await _set_channel_var(client, channel_id, "AMDMS",     str(result.get("latency_ms", 0)))

    # Continuar el dialplan real en la etiqueta que sigue a Stasis() — con
    # `label`, no un número de prioridad fijo, para no depender de contar
    # líneas del dialplan (ver dialplan de referencia en CLAUDE.md).
    resp = await client.post(
        f"/channels/{channel_id}/continue",
        params={"context": "default", "extension": "8379", "label": "after-ari"},
    )
    if resp.status_code not in (200, 204):
        log.error("[%s] ARI: no se pudo continuar el dialplan (%s): %s", session_id, resp.status_code, resp.text)

    log.info("[%s] ARI: → %s layer=%s %sms", session_id,
              result.get("status"), result.get("layer_used"), result.get("latency_ms"))


async def _run_audiosocket_mode(
    client: httpx.AsyncClient, session_id: str, call_uuid: str, snoop_id: str,
    voxidet_client: dict, channel_id: str, channelvars: dict | None = None,
) -> dict | None:
    """Reusa chan_audiosocket + el servidor TCP que ya corre siempre
    (audiosocket_server.py, sin cambios ahí) — requiere ese módulo cargado
    en Asterisk (ver ARI_MEDIA_MODE en config.py)."""
    r = await _get_redis()

    # Mismo formato exacto que POST /amd/audiosocket/register (app/api/amd.py)
    # — así audiosocket_server.py lo resuelve sin ningún cambio de su lado.
    await r.setex(
        f"audiosocket:pending:{call_uuid}",
        _PENDING_TTL,
        json.dumps({
            "client":      voxidet_client,
            "call_id":     channel_id[:100],
            "caller_id":   await _get_channel_var(client, channel_id, "phone_number", channelvars),
            "lead_id":     await _get_channel_var(client, channel_id, "lead_id", channelvars),
            "campaign_id": await _get_channel_var(client, channel_id, "campaign_id", channelvars),
            "list_id":     await _get_channel_var(client, channel_id, "list_id", channelvars),
        }, default=str),
    )

    if not settings.AUDIOSOCKET_HOST:
        log.error("[%s] ARI: AUDIOSOCKET_HOST no configurado — no se puede originar el canal AudioSocket", session_id)
        await _hangup_channel(client, snoop_id)
        return None

    endpoint = f"AudioSocket/{call_uuid}/{settings.AUDIOSOCKET_HOST}:{settings.AUDIOSOCKET_PORT}"
    resp = await client.post(
        "/channels",
        params={"endpoint": endpoint, "app": settings.ARI_APP, "appArgs": "audiosocket"},
    )
    if resp.status_code not in (200, 201):
        log.error("[%s] ARI: no se pudo originar canal AudioSocket (%s): %s", session_id, resp.status_code, resp.text)
        await _hangup_channel(client, snoop_id)
        return None
    audiosocket_channel_id = resp.json().get("id")

    bridge_id = await _bridge_pair(client, snoop_id, audiosocket_channel_id)
    log.info("[%s] ARI (audiosocket): snoop=%s audiosocket=%s bridge=%s cliente=%s — analizando",
              session_id, snoop_id, audiosocket_channel_id, bridge_id, voxidet_client.get("name"))

    # audiosocket_server.py corta la conexión TCP apenas decide y RECIÉN
    # DESPUÉS guarda el resultado en Redis — ventana corta donde el canal ya
    # puede estar destruido pero el resultado todavía no está escrito.
    result = None
    elapsed = 0.0
    while elapsed < _RESULT_POLL_TIMEOUT:
        raw = await r.get(f"audiosocket:result:{call_uuid}")
        if raw:
            result = json.loads(raw)
            break
        await asyncio.sleep(_RESULT_POLL_INTERVAL)
        elapsed += _RESULT_POLL_INTERVAL

    await _hangup_channel(client, audiosocket_channel_id)
    await _hangup_channel(client, snoop_id)
    await _delete_bridge(client, bridge_id)

    if result is None:
        log.warning("[%s] ARI (audiosocket): sin resultado en Redis tras %.1fs — ERROR", session_id, _RESULT_POLL_TIMEOUT)
    return result


async def _run_rtp_mode(
    client: httpx.AsyncClient, session_id: str, call_uuid: str, snoop_id: str,
    voxidet_client: dict,
) -> dict | None:
    """Usa externalMedia (RTP crudo por UDP, nativo de ARI) — no necesita
    ningún módulo extra en Asterisk. Corre la detección EN ESTE PROCESO
    (ver _load_models() y app/ari/rtp_media.py)."""
    from app.ari.rtp_media import create_rtp_session
    from app.core.silero_vad import make_detector
    from app.db.providers import get_vad_engine
    from app.api.stream import _decide_and_send, get_cached_vad_engine

    done = asyncio.Event()
    audio_bytes = 0

    vad_engine = get_cached_vad_engine() or await get_vad_engine()
    detector = make_detector(vad_engine)

    def on_audio(chunk: bytes) -> None:
        nonlocal audio_bytes
        audio_bytes += len(chunk)
        if detector.feed(chunk) and not done.is_set():
            done.set()

    rtp = await create_rtp_session(on_audio)

    resp = await client.post(
        "/channels/externalMedia",
        params={
            "app": settings.ARI_APP,
            "external_host": f"{_local_ip_hint()}:{rtp.port}",
            "format": "slin",
            "encapsulation": "rtp",
            "transport": "udp",
            "connection_type": "client",
            "direction": "both",
        },
    )
    if resp.status_code not in (200, 201):
        log.error("[%s] ARI (rtp): no se pudo crear externalMedia (%s): %s", session_id, resp.status_code, resp.text)
        rtp.close()
        await _hangup_channel(client, snoop_id)
        return None
    media_channel_id = resp.json().get("id")

    bridge_id = await _bridge_pair(client, snoop_id, media_channel_id)
    log.info("[%s] ARI (rtp): snoop=%s externalMedia=%s puerto_udp=%d bridge=%s cliente=%s — analizando",
              session_id, snoop_id, media_channel_id, rtp.port, bridge_id, voxidet_client.get("name"))

    t0 = time.monotonic()
    stop_silence = asyncio.Event()

    async def silence_loop() -> None:
        try:
            while not stop_silence.is_set():
                rtp.send_silence()
                await asyncio.sleep(0.02)
        except Exception:
            pass

    sil_task = asyncio.create_task(silence_loop())

    try:
        remaining = _MAX_SECS
        while remaining > 0:
            try:
                await asyncio.wait_for(done.wait(), timeout=remaining)
                break
            except asyncio.TimeoutError:
                break
            finally:
                remaining = _MAX_SECS - (time.monotonic() - t0)
    finally:
        stop_silence.set()
        sil_task.cancel()
        rtp.close()
        await _hangup_channel(client, media_channel_id)
        await _hangup_channel(client, snoop_id)
        await _delete_bridge(client, bridge_id)

    result, layer, transcript = "UNKNOWN", 1, ""
    if audio_bytes > 0:
        keywords_mode = voxidet_client.get("keywords_mode", "global")
        ckw_human, ckw_voicemail = set(), set()
        if keywords_mode == "custom":
            try:
                from app.db.client_keywords import get_cached_client_keywords
                ckw_human, ckw_voicemail = await get_cached_client_keywords(voxidet_client["id"])
            except Exception:
                pass
        result, layer, transcript = await _decide_and_send(
            None, detector, session_id, t0, voxidet_client.get("provider", "groq"),
            extra_human=ckw_human or None, extra_voicemail=ckw_voicemail or None,
            aggressive=voxidet_client.get("amd_bias") == "aggressive",
        )

    latency_ms = int((time.monotonic() - t0) * 1000)

    try:
        from app.db.logs import save_log
        await save_log(
            client_id=voxidet_client["id"], call_id=session_id[:100], caller_id="",
            result=result, layer=layer, mode="ari", latency_ms=latency_ms,
            audio_secs=round(audio_bytes / (8000 * 2), 2),
            provider=voxidet_client.get("provider", "") if layer == 2 else "",
            transcript=transcript[:200] if transcript else "",
            param2=session_id, beep_detected=detector.tone_detected(),
        )
    except Exception as e:
        log.warning("[%s] ARI (rtp): save_log falló (no bloquea el resultado): %s", session_id, e)

    return {"status": result, "layer_used": layer, "latency_ms": latency_ms}


def _local_ip_hint() -> str:
    """El host que le pasamos a externalMedia para que Asterisk nos mande
    el RTP — reusa AUDIOSOCKET_HOST (mismo IP interno que ya usa el modo
    Audiosocket clásico), aunque acá el puerto es dinámico por llamada, no
    el fijo de AUDIOSOCKET_PORT."""
    return settings.AUDIOSOCKET_HOST or "127.0.0.1"


async def _bridge_pair(client: httpx.AsyncClient, channel_a: str, channel_b: str) -> str | None:
    resp = await client.post("/bridges", params={"type": "mixing"})
    bridge_id = resp.json().get("id") if resp.status_code in (200, 201) else None
    if not bridge_id:
        log.error("ARI: no se pudo crear el bridge para %s + %s", channel_a, channel_b)
        return None
    await client.post(f"/bridges/{bridge_id}/addChannel", params={"channel": f"{channel_a},{channel_b}"})
    return bridge_id


async def _delete_bridge(client: httpx.AsyncClient, bridge_id: str | None) -> None:
    if not bridge_id:
        return
    try:
        await client.delete(f"/bridges/{bridge_id}")
    except Exception:
        pass


async def _get_redis():
    from app.cache.client_cache import get_redis
    return await get_redis()


async def _handle_event(client: httpx.AsyncClient, event: dict) -> None:
    if event.get("type") != "StasisStart":
        return
    channel = event.get("channel") or {}
    channel_id = channel.get("id")
    args = event.get("args") or []

    # Los canales Snoop/AudioSocket/externalMedia que originamos nosotros
    # mismos también entran a esta app de Stasis (mismo `app`) — se
    # identifican por el appArgs que les pasamos al crearlos, para no
    # reprocesarlos como si fueran una llamada nueva.
    if args and args[0] in ("audiosocket", "snoop"):
        log.debug("ARI: StasisStart de canal propio (%s, args=%s), ignorado", channel_id, args)
        return

    log.info("ARI: StasisStart canal real=%s (%s)", channel_id, channel.get("name", ""))
    asyncio.create_task(_process_call(client, channel_id, channel.get("channelvars")))


async def _apply_db_overrides() -> None:
    """Mismo mecanismo que resolve_server_url()/app/api/install.py para
    public_url: el panel (Sistema → Modo ARI) guarda en MySQL (app_settings)
    porque no puede reescribir credentials.conf (env_file: en
    docker-compose.yml nunca lo monta como archivo). Se aplica UNA vez al
    arrancar, mutando `settings` in-place — el resto del módulo ya lee todo
    desde `settings.ARI_*`, así que no hace falta tocar nada más. Un cambio
    desde el panel requiere reiniciar este contenedor para tomar efecto (no
    es hot-reload — es una conexión WebSocket persistente de larga vida)."""
    try:
        from app.db.settings import get_setting
        overrides = {
            "ARI_URL":          await get_setting("ari_url"),
            "ARI_USER":         await get_setting("ari_user"),
            "ARI_PASSWORD":     await get_setting("ari_password"),
            "ARI_APP":          await get_setting("ari_app"),
            "ARI_MEDIA_MODE":   await get_setting("ari_media_mode"),
            "AUDIOSOCKET_HOST": await get_setting("audiosocket_host"),
        }
        for key, value in overrides.items():
            if value:
                setattr(settings, key, value)
    except Exception as e:
        log.warning("ARI: no se pudo leer overrides de MySQL (se sigue con credentials.conf/env): %s", e)


async def run() -> None:
    """Loop principal — conecta al WebSocket de eventos de ARI y despacha
    StasisStart. Reconecta solo si se corta (Asterisk reiniciando, red, etc)."""
    await _apply_db_overrides()

    if not settings.ARI_URL or not settings.ARI_PASSWORD:
        log.error("ARI: ARI_URL/ARI_PASSWORD no configurados — el controlador no arranca "
                   "(ver README.md § Modo ARI (experimental)).")
        return

    _load_models()

    ws_url = (
        settings.ARI_URL.replace("http://", "ws://").replace("https://", "wss://")
        + f"/ari/events?api_key={settings.ARI_USER}:{settings.ARI_PASSWORD}"
          f"&app={settings.ARI_APP}&subscribeAll=true"
    )

    async with _rest_client() as client:
        while True:
            try:
                log.info("ARI: conectando a %s (app=%s, modo=%s)", settings.ARI_URL, settings.ARI_APP, settings.ARI_MEDIA_MODE)
                async with websockets.connect(ws_url) as ws:
                    log.info("ARI: conectado — esperando eventos de Stasis")
                    async for raw in ws:
                        try:
                            event = json.loads(raw)
                        except Exception:
                            continue
                        asyncio.create_task(_handle_event(client, event))
            except Exception as e:
                log.error("ARI: conexión perdida o falló (%s) — reintentando en 5s", e)
                await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    asyncio.run(run())
