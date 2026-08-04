# AGI script template. Placeholders replaced by /install endpoint:
#   __SERVER__  → URL pública del VPS (ej: http://1.2.3.4:8000)
#   __APIKEY__  → API key del cliente
#   __CLIENT__  → nombre del cliente
#   __VERSION__ → hash del template (auto-update cuando cambia)

TEMPLATE = r"""#!/usr/bin/env python3
# amd_ia.agi — AMD Detection IA (batch + stream, auto-update)
# Cliente : __CLIENT__
# Servidor: __SERVER__
# Sin dependencias externas — solo Python 3 stdlib
#
# Dialplan: AGI(amd_ia.agi) o EAGI(amd_ia.agi)
# Al inicio consulta /amd/check: obtiene modo y versión.
# Si hay nueva versión, se auto-actualiza y re-ejecuta.
#
import sys, os, select, socket, struct, secrets, tempfile, json, base64
from time import time
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

_SERVER  = "__SERVER__"
_API_KEY = "__APIKEY__"
_VERSION = "__VERSION__"
_TIMEOUT = 8


# ── AGI helpers ──────────────────────────────────────────────────────────────

def _send(cmd):
    sys.stdout.write(cmd + "\n"); sys.stdout.flush()
    return sys.stdin.readline().strip()

def _set(name, value): _send(f"SET VARIABLE {name} {value}")
def _log(msg):         _send(f'VERBOSE "{msg}" 1')

def _get_var(name):
    r = _send(f"GET VARIABLE {name}")
    if "(" in r and ")" in r:
        return r.split("(", 1)[1].rsplit(")", 1)[0].strip()
    return ""

def _record(path, fmt, esc, ms, sil):
    _send(f"RECORD FILE {path} {fmt} {esc} {ms} s={sil}")


# ── Check mode + auto-update ─────────────────────────────────────────────────

def _check_server():
    '''Consulta /amd/check, devuelve (mode, need_update).'''
    try:
        req = Request(
            f"{_SERVER}/amd/check",
            headers={"X-API-Key": _API_KEY},
        )
        with urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
        mode        = data.get("mode", "batch")
        server_ver  = data.get("version", "")
        need_update = bool(server_ver and server_ver != _VERSION)
        return mode, need_update
    except Exception:
        return "batch", False


def _self_update():
    '''Descarga la nueva version del AGI y la deja lista para la PRÓXIMA
    llamada — nunca se reinicia a mitad de ejecución. AGI manda su bloque de
    variables de entorno (uniqueid, callerid...) una sola vez por stdin, al
    arrancar; un os.execv() a mitad de camino haría que el proceso nuevo
    intente releer ese mismo handshake, que Asterisk no vuelve a mandar, y
    quede colgado sin llegar nunca a setear AMDSTATUS (visto en producción:
    "AMD:  capa= ms" en blanco, ni un solo log de esta llamada).'''
    path = os.path.abspath(__file__)
    # Nombre único por proceso — __file__ es el mismo para todas las llamadas
    # AGI concurrentes en el mismo nodo Asterisk, así que un ".tmp" fijo
    # compartido entre procesos puede hacer que uno pise el archivo temporal
    # de otro a mitad de escritura (visto como riesgo real con 150+ agentes,
    # todos detectando la misma versión nueva al mismo tiempo tras un deploy).
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        req = Request(f"{_SERVER}/amd/update", headers={"X-API-Key": _API_KEY})
        with urlopen(req, timeout=8) as r:
            new_code = r.read()
        with open(tmp, "wb") as f:
            f.write(new_code)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
        # Sin re-exec — la llamada en curso sigue con el código ya cargado en
        # memoria (este mismo proceso). La próxima llamada que arranque un
        # proceso nuevo del AGI ya lee el archivo actualizado desde el disco.
    except Exception:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except Exception: pass


# ── Batch mode ───────────────────────────────────────────────────────────────

def _run_batch(uid, phone, lead_id, campaign_id, list_id):
    tmp = tempfile.mktemp(prefix=f"amd_{uid}_")
    wav = tmp + ".wav"
    try:
        # s=1500 (antes 1000): Asterisk corta la grabación si detecta ese
        # tiempo de silencio propio ANTES de llegar a los 2500ms totales. Con
        # 1000ms, un humano que tarda un poco en reaccionar al atender
        # quedaba con el WAV truncado/vacío antes de decir una palabra — nuestro
        # propio análisis de energía clasifica bien lo que recibe, el problema
        # era recibir menos audio del real. Reportado en producción con más
        # agentes conectados (ver CHANGELOG).
        _record(tmp, "wav", "#", 2500, 1500)

        if not os.path.exists(wav):
            _log("AMD-IA batch: no wav — UNKNOWN")
            _set("AMDSTATUS", "UNKNOWN")
            return

        with open(wav, "rb") as f:
            audio = f.read()

        bnd  = "amdbound" + secrets.token_hex(6)
        body = (
            f"--{bnd}\r\n"
            f"Content-Disposition: form-data; name=\"audio\"; filename=\"audio.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode() + audio + f"\r\n--{bnd}--\r\n".encode()

        req = Request(
            f"{_SERVER}/amd",
            data=body,
            headers={
                "Content-Type":  f"multipart/form-data; boundary={bnd}",
                "X-API-Key":     _API_KEY,
                "X-Call-ID":     uid[:100],
                "X-Caller-ID":   phone[:50],
                "X-Lead-ID":     lead_id[:50],
                "X-Campaign-ID": campaign_id[:50],
                "X-List-ID":     list_id[:50],
            },
        )
        with urlopen(req, timeout=_TIMEOUT) as r:
            d = json.loads(r.read())

        status = d.get("result", d.get("status", "UNKNOWN"))
        layer  = d.get("layer_used", d.get("layer", 0))
        ms     = d.get("latency_ms", 0)
        _log(f"AMD-IA batch: {status} layer={layer} {ms}ms")
        _set("AMDSTATUS", status)
        _set("AMDLAYER",  str(layer))
        _set("AMDMS",     str(ms))

    except (URLError, HTTPError, OSError) as e:
        _log(f"AMD-IA batch error: {e}")
        _set("AMDSTATUS", "ERROR")
    finally:
        if os.path.exists(wav):
            try: os.unlink(wav)
            except Exception: pass


# ── Stream mode (EAGI + WebSocket raw) ───────────────────────────────────────

def _ws_handshake(sock, host, path, uid, phone, lead_id, campaign_id, list_id):
    # RFC 6455 exige que Sec-WebSocket-Key sea base64 ESTÁNDAR (alfabeto +//)
    # de 16 bytes random. secrets.token_urlsafe() usa el alfabeto URL-safe
    # (-/_) y además viene sin padding — se le agregaba "==" a mano asumiendo
    # que solo faltaba el padding, pero el alfabeto ya estaba mal. Cuando los
    # 16 bytes random caían en un '-' o '_' (~50% de las veces), el servidor
    # WS rechazaba el handshake con 400 Bad Request — root cause de los
    # "connection rejected (400 Bad Request)" intermitentes vistos en producción.
    key    = base64.b64encode(os.urandom(16)).decode()
    params = (f"api_key={_API_KEY}&call_id={uid}&caller_id={phone}"
              f"&lead_id={lead_id}&campaign_id={campaign_id}&list_id={list_id}")
    req = (
        f"GET {path}?{params} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            return False
        resp += chunk
    return b"101" in resp.split(b"\r\n", 1)[0]


def _ws_frame(payload: bytes, binary=True) -> bytes:
    fin_op   = 0x82 if binary else 0x81
    mask_key = os.urandom(4)
    plen     = len(payload)
    if plen < 126:
        header = struct.pack("BB", fin_op, 0x80 | plen)
    elif plen < 65536:
        header = struct.pack("!BBH", fin_op, 0xFE, plen)
    else:
        header = struct.pack("!BBQ", fin_op, 0xFF, plen)
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return header + mask_key + masked


def _ws_recv(sock):  # devuelve bytes o None — sin anotación por compatibilidad con Python 3.6 (Asterisk boxes)
    try:
        hdr = sock.recv(2)
        if len(hdr) < 2:
            return None
        plen = hdr[1] & 0x7F
        if plen == 126:
            plen = struct.unpack("!H", sock.recv(2))[0]
        elif plen == 127:
            plen = struct.unpack("!Q", sock.recv(8))[0]
        data = b""
        while len(data) < plen:
            chunk = sock.recv(plen - len(data))
            if not chunk:
                return None
            data += chunk
        return data
    except Exception:
        return None


def _run_stream(uid, phone, lead_id, campaign_id, list_id):
    from urllib.parse import urlparse
    parsed = urlparse(_SERVER)
    host   = parsed.hostname
    port   = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        sock = socket.create_connection((host, port), timeout=5)
    except Exception as e:
        _log(f"AMD-IA stream: conexión fallida ({e}) — fallback batch")
        return _run_batch(uid, phone, lead_id, campaign_id, list_id)

    result = None
    try:
        if not _ws_handshake(sock, f"{host}:{port}", "/amd/stream",
                             uid, phone, lead_id, campaign_id, list_id):
            _log("AMD-IA stream: WS handshake falló — fallback batch")
            sock.close()
            return _run_batch(uid, phone, lead_id, campaign_id, list_id)

        # Autenticación: el servidor NO lee los query params de la URL de
        # conexión — espera un mensaje de texto JSON como PRIMER frame
        # después del handshake. Sin esto, el servidor espera 5s y cierra
        # por timeout de auth sin procesar nada (visto en producción:
        # "connection open" seguido de "connection closed" sin ningún log
        # intermedio, ni un "autenticado").
        auth = json.dumps({
            "api_key":     _API_KEY,
            "call_id":     uid,
            "caller_id":   phone,
            "lead_id":     lead_id,
            "campaign_id": campaign_id,
            "list_id":     list_id,
        })
        sock.sendall(_ws_frame(auth.encode(), binary=False))

        ack_raw = _ws_recv(sock)
        ack = json.loads(ack_raw) if ack_raw else {}
        if not ack.get("ok"):
            _log(f"AMD-IA stream: auth rechazada ({ack.get('error', 'sin respuesta')}) — fallback batch")
            sock.close()
            return _run_batch(uid, phone, lead_id, campaign_id, list_id)

        sock.setblocking(False)
        fd_sock  = sock.fileno()
        deadline = time() + 8.0

        while time() < deadline:
            r, _, _ = select.select([3, fd_sock], [], [], 0.05)

            if fd_sock in r:
                frame = _ws_recv(sock)
                if frame:
                    try: result = json.loads(frame)
                    except Exception: pass
                break

            if 3 in r:
                chunk = os.read(3, 320)
                if not chunk:
                    break
                try:
                    sock.setblocking(True)
                    sock.sendall(_ws_frame(chunk, binary=True))
                    sock.setblocking(False)
                except Exception:
                    break

    except Exception as e:
        # Sin este except, cualquier fallo acá (ej. select() sobre un fd3 en
        # mal estado) se propaga sin atrapar, tumba el script entero y
        # AMDSTATUS queda sin setear — visto en producción en vd1atk1 (NoOp
        # vacío, ni un log de "AMD-IA start"). result queda None → cae al
        # bloque de abajo, que ya pone AMDSTATUS=ERROR.
        _log(f"AMD-IA stream error: {e}")
    finally:
        try: sock.close()
        except Exception: pass

    if result:
        status = result.get("status", "UNKNOWN")
        layer  = result.get("layer", 0)
        ms     = result.get("latency_ms", 0)
        _log(f"AMD-IA stream: {status} layer={layer} {ms}ms")
        _set("AMDSTATUS", status)
        _set("AMDLAYER",  str(layer))
        _set("AMDMS",     str(ms))
    else:
        _log("AMD-IA stream: sin respuesta — ERROR")
        _set("AMDSTATUS", "ERROR")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    env = {}
    while True:
        line = sys.stdin.readline()
        if not line or line == "\n":
            break
        if ":" in line:
            k, v = line.strip().split(":", 1)
            env[k.strip()] = v.strip()

    uid         = env.get("agi_uniqueid", "unknown")
    cid         = env.get("agi_callerid", "unknown")
    phone       = _get_var("phone_number") or _get_var("CALLED") or cid
    lead_id     = _get_var("lead_id")
    campaign_id = _get_var("campaign_id")
    list_id     = _get_var("list_id")

    mode, need_update = _check_server()

    if need_update:
        _log(f"AMD-IA: nueva version disponible — descargando para la próxima llamada")
        _self_update()
        # _self_update() nunca reinicia este proceso — la llamada actual
        # sigue con el código ya cargado en memoria (viejo o nuevo, según si
        # ya se había actualizado en una llamada anterior). El archivo en
        # disco queda listo para la PRÓXIMA vez que Asterisk lance el AGI.

    _log(f"AMD-IA start uid={uid} mode={mode}")

    try:
        if mode == "stream":
            _run_stream(uid, phone, lead_id, campaign_id, list_id)
        else:
            _run_batch(uid, phone, lead_id, campaign_id, list_id)
    except Exception as e:
        # Red de seguridad final — cualquier excepción no atrapada más abajo
        # (batch mode, o algo inesperado) deja AMDSTATUS=ERROR en vez de sin
        # setear. El dialplan siempre recibe algo para decidir con GotoIf.
        _log(f"AMD-IA error no atrapado: {e}")
        _set("AMDSTATUS", "ERROR")


if __name__ == "__main__":
    main()
"""
