"""
api/stream.py — WebSocket AMD en tiempo real + transcripción Groq.

Flujo:
  1. EAGI abre WebSocket → se asigna session_id único
  2. Auth con api_key (primer mensaje JSON)
  3. Audio chunks llegan en tiempo real (20ms cada uno)
  4. VAD engine (StreamDetector o Silero) detecta actividad de voz / silencio
  5. Al detectar silencio: envía audio acumulado al proveedor configurado (batch, ~100ms)
  6. Resultado: HUMAN/VOICEMAIL + transcript → EAGI + logs + panel web
"""

import asyncio
import hashlib
import io
import json
import logging
import secrets
import time
import wave

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.cache.client_cache import (
    check_and_increment_limit,
    get_client_cached,
    get_redis,
)
from app.db.providers import get_provider_model
from app.db.provider_keys import get_active_keys
from app.core.amd_engine import _classify_transcript
from app.core.http_client import get_http_client
from app.core.ip_utils import ip_allowed
from app.core.local_asr import transcribe_vosk, transcribe_sherpa, vosk_loaded, sherpa_loaded, make_vosk_stream_session, _build_vosk_grammar
from app.core.silero_vad import silero_loaded, make_detector
from app.db.client_keywords import get_cached_client_keywords
from app.db.logs import save_log
from app.db.provider_stats import record_stat as _record_stat
from app.db.providers import get_vad_engine as _get_vad_engine

log    = logging.getLogger("voxidet.stream")
router = APIRouter()

# Cache en memoria para evitar query DB/Redis en cada conexión WebSocket
_cached_vad_engine: str | None = None


def refresh_vad_engine_cache(engine: str) -> None:
    global _cached_vad_engine
    _cached_vad_engine = engine


# ── Groq transcripción ────────────────────────────────────────────────────────

def _pcm_to_wav(pcm: bytes, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)       # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


async def _get_groq_keys() -> list[dict]:
    return await get_active_keys("groq")

async def _get_fireworks_keys() -> list[dict]:
    return await get_active_keys("fireworks")

async def _get_together_keys() -> list[dict]:
    return await get_active_keys("together")

async def _get_openai_keys() -> list[dict]:
    return await get_active_keys("openai")

def _build_groq_prompt() -> str:
    """
    initial_prompt para Whisper — lista de palabras esperadas separadas por coma.
    Whisper las usa para sesgar el vocabulario hacia esas palabras sin restricción dura.
    Se construye en tiempo real desde el keyword_cache (DB) para ser siempre actual.
    """
    from app.core import keyword_cache
    words = sorted(keyword_cache.get_human() | keyword_cache.get_voicemail())
    return ", ".join(words) + "."


async def _log_groq_keys() -> None:
    keys = await _get_groq_keys()
    log.info("Groq: %d key(s) — %d RPM efectivos", len(keys), len(keys) * 20) if keys \
        else log.warning("Groq: sin API key")
    dg = await _get_deepgram_keys()
    log.info("Deepgram: %d key(s) — %d concurrentes máx", len(dg), len(dg) * 50) if dg \
        else log.warning("Deepgram: sin API key")
    fw = await _get_fireworks_keys()
    log.info("Fireworks: %d key(s)", len(fw)) if fw \
        else log.info("Fireworks: sin API key (desactivado)")
    tg = await _get_together_keys()
    log.info("Together AI: %d key(s)", len(tg)) if tg \
        else log.info("Together AI: sin API key (desactivado)")
    oa = await _get_openai_keys()
    log.info("OpenAI: %d key(s)", len(oa)) if oa \
        else log.info("OpenAI: sin API key (desactivado)")
    log.info("Vosk local: %s", "cargado" if vosk_loaded() else "no configurado")
    log.info("Sherpa local: %s", "cargado" if sherpa_loaded() else "no configurado")
    log.info("Silero VAD: %s", "cargado" if silero_loaded() else "no configurado")


_GROQ_FREE_RPM = 20  # límite del plan free de Groq por key


def _pick_key_index(session_id: str, keys: list) -> int:
    """Posición (no id) dentro de `keys` — determinística por session_id, para que
    los reintentos de una misma llamada empiecen siempre por la misma key."""
    return int(hashlib.md5(session_id.encode()).hexdigest(), 16) % len(keys)


async def _groq_claim_slot(idx: int) -> bool:
    """Incrementa el contador de esta key en Redis. Devuelve True si aún hay cupo."""
    try:
        r = await get_redis()
        rkey = f"groq:rpm:{idx}"
        count = await r.incr(rkey)
        if count == 1:
            await r.expire(rkey, 60)  # ventana de 60s — se resetea sola
        return count <= _GROQ_FREE_RPM
    except Exception:
        return True  # si Redis falla, dejar pasar


async def _get_deepgram_keys() -> list[dict]:
    return await get_active_keys("deepgram")

_dg_idx = 0  # round-robin por worker — Deepgram rota por concurrencia, no RPM


async def _transcribe_deepgram(pcm: bytes, session_id: str) -> str:
    """Envía audio a Deepgram Nova-2. Rota entre keys por concurrencia (límite: 50/key)."""
    global _dg_idx
    keys = await _get_deepgram_keys()
    if not keys or len(pcm) < 1600:
        return ""
    wav   = _pcm_to_wav(pcm)
    start = _pick_key_index(session_id, keys)

    for attempt in range(len(keys)):
        pos = (start + attempt) % len(keys)
        idx, key = keys[pos]["id"], keys[pos]["key"]
        try:
            client = get_http_client()
            resp = await client.post(
                f"https://api.deepgram.com/v1/listen?model={await get_provider_model('deepgram', idx)}&language=es&smart_format=false",
                headers={
                    "Authorization": f"Token {key}",
                    "Content-Type": "audio/wav",
                },
                content=wav,
                timeout=5.0,
            )
            if resp.status_code == 200:
                channels = resp.json().get("results", {}).get("channels", [])
                if channels:
                    alts = channels[0].get("alternatives", [])
                    return alts[0].get("transcript", "").strip() if alts else ""
                return ""
            if resp.status_code == 429:
                log.warning("[%s] Deepgram key[%d] límite concurrencia — rotando", session_id, idx)
                continue
            log.warning("[%s] Deepgram %s: %s", session_id, resp.status_code, resp.text[:200])
            return ""
        except Exception as e:
            log.warning("[%s] Deepgram error: %s", session_id, e)
            return ""
    return ""


async def _transcribe_groq(pcm: bytes, session_id: str) -> str:
    """Envía audio a Groq Whisper. Distribuye carga entre todas las keys usando
    contadores de RPM en Redis — sin esperar 429 para rotar."""
    keys = await _get_groq_keys()
    if not keys or len(pcm) < 1600:
        return ""
    wav   = _pcm_to_wav(pcm)
    start = _pick_key_index(session_id, keys)

    for attempt in range(len(keys)):
        pos = (start + attempt) % len(keys)
        idx = keys[pos]["id"]
        has_slot = await _groq_claim_slot(idx)
        if not has_slot:
            log.debug("[%s] Groq key[%d] al límite RPM — rotando", session_id, idx)
            continue
        key = keys[pos]["key"]
        try:
            client = get_http_client()
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", wav, "audio/wav")},
                data={
                    "model":           await get_provider_model("groq", idx),
                    "language":        "es",
                    "prompt":          _build_groq_prompt(),
                    "response_format": "json",
                },
                timeout=5.0,
            )
            if resp.status_code == 200:
                return resp.json().get("text", "").strip()
            if resp.status_code == 429:
                # Forzar el contador al límite para que los demás workers también roten
                try:
                    r = await get_redis()
                    await r.set(f"groq:rpm:{idx}", _GROQ_FREE_RPM + 1, ex=60)
                except Exception:
                    pass
                log.warning("[%s] Groq key[%d] 429 inesperado — contador sincronizado", session_id, idx)
                continue
            log.warning("[%s] Groq %s: %s", session_id, resp.status_code, resp.text[:200])
            return ""
        except Exception as e:
            log.warning("[%s] Groq error: %s", session_id, e)
            return ""
    return ""


async def _transcribe_openai(pcm: bytes, session_id: str) -> str | None:
    """OpenAI gpt-4o-transcribe / gpt-4o-mini-transcribe. Retorna None en error de servicio."""
    keys = await _get_openai_keys()
    if not keys or len(pcm) < 1600:
        return ""
    wav   = _pcm_to_wav(pcm)
    pos   = _pick_key_index(session_id, keys)
    idx, key = keys[pos]["id"], keys[pos]["key"]
    model = await get_provider_model("openai", idx)
    try:
        # gpt-4o-transcribe/mini no soporta prompt como Whisper — lo alucina como audio
        # Solo enviamos prompt para whisper-1 que lo usa correctamente como vocabulary hint
        data: dict = {"model": model, "language": "es", "response_format": "json"}
        if model == "whisper-1":
            data["prompt"] = _build_groq_prompt()
        client = get_http_client()
        resp = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": ("audio.wav", wav, "audio/wav")},
            data=data,
            timeout=8.0,
        )
        if resp.status_code == 200:
            return resp.json().get("text", "").strip()
        log.warning("[%s] OpenAI %s — activando fallback", session_id, resp.status_code)
        return None  # 429, 5xx → fallback
    except Exception as e:
        log.warning("[%s] OpenAI error: %s — activando fallback", session_id, e)
        return None


async def _transcribe_together(pcm: bytes, session_id: str) -> str | None:
    """Together AI Whisper/Parakeet. Retorna None en error de servicio para activar fallback."""
    keys = await _get_together_keys()
    if not keys or len(pcm) < 1600:
        return ""
    wav   = _pcm_to_wav(pcm)
    pos   = _pick_key_index(session_id, keys)
    idx, key = keys[pos]["id"], keys[pos]["key"]
    model = await get_provider_model("together", idx)
    try:
        # prompt solo para modelos Whisper (Parakeet CTC no lo soporta)
        req_data: dict = {"model": model, "language": "es", "response_format": "json"}
        if "whisper" in model.lower():
            raw_prompt = _build_groq_prompt()
            # Whisper limit ~224 tokens ≈ 800 chars en español con tildes
            req_data["prompt"] = raw_prompt[:800]
        client = get_http_client()
        resp = await client.post(
            "https://api.together.xyz/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": ("audio.wav", wav, "audio/wav")},
            data=req_data,
            timeout=5.0,
        )
        if resp.status_code == 200:
            return resp.json().get("text", "").strip()
        log.warning("[%s] Together %s: %s — activando fallback", session_id, resp.status_code, resp.text[:200])
        return None  # 400/429/5xx → fallback
    except Exception as e:
        log.warning("[%s] Together error: %s — activando fallback", session_id, e)
        return None


async def _transcribe_fireworks(pcm: bytes, session_id: str) -> str | None:
    """Fireworks Whisper. Retorna None en error de servicio para activar fallback."""
    keys = await _get_fireworks_keys()
    if not keys or len(pcm) < 1600:
        return ""
    wav   = _pcm_to_wav(pcm)
    pos   = _pick_key_index(session_id, keys)
    idx, key = keys[pos]["id"], keys[pos]["key"]
    model = await get_provider_model("fireworks", idx)
    try:
        client = get_http_client()
        resp = await client.post(
            "https://api.fireworks.ai/inference/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": ("audio.wav", wav, "audio/wav")},
            data={"model": model, "language": "es", "prompt": _build_groq_prompt(), "response_format": "json"},
            timeout=5.0,
        )
        if resp.status_code == 200:
            return resp.json().get("text", "").strip()
        log.warning("[%s] Fireworks %s — activando fallback", session_id, resp.status_code)
        return None  # 429, 5xx → fallback
    except Exception as e:
        log.warning("[%s] Fireworks error: %s — activando fallback", session_id, e)
        return None


# ── Deepgram v2: streaming WebSocket en tiempo real ──────────────────────────

class _DeepgramStreamSession:
    """Abre un WebSocket a Deepgram al inicio de la llamada y recibe audio en
    tiempo real — transcript disponible en ~200ms sin esperar silencio."""

    _BASE = (
        "wss://api.deepgram.com/v1/listen"
        "?language=es&encoding=linear16"
        "&sample_rate=8000&channels=1&interim_results=false&smart_format=false"
    )

    def __init__(self, key: str, session_id: str, model: str = "nova-2"):
        self._key       = key
        self._sid       = session_id
        self._url       = f"{self._BASE}&model={model}"
        self._ws        = None
        self._parts: list[str] = []
        self._recv_task: asyncio.Task | None = None
        self._done      = asyncio.Event()

    async def connect(self) -> None:
        import websockets
        headers = {"Authorization": f"Token {self._key}"}
        try:
            self._ws = await websockets.connect(self._url, additional_headers=headers, open_timeout=5)
        except TypeError:
            self._ws = await websockets.connect(self._url, extra_headers=headers, open_timeout=5)
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                    if data.get("type") == "Results":
                        alts = data.get("channel", {}).get("alternatives", [])
                        if alts:
                            t = alts[0].get("transcript", "").strip()
                            if t:
                                self._parts.append(t)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._done.set()

    async def send(self, pcm_chunk: bytes) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(pcm_chunk)
        except Exception:
            pass

    async def finish(self) -> str:
        """Cierra el stream, espera últimos resultados y devuelve el transcript."""
        try:
            if self._ws:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await asyncio.wait_for(self._done.wait(), timeout=1.5)
        except Exception:
            pass
        finally:
            if self._recv_task:
                self._recv_task.cancel()
            try:
                if self._ws:
                    await self._ws.close()
            except Exception:
                pass
        return " ".join(self._parts).strip()

    async def cancel(self) -> None:
        """Cancela sin esperar — usado en cleanup."""
        if self._recv_task:
            self._recv_task.cancel()
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass


# ── Auth ──────────────────────────────────────────────────────────────────────

async def _auth(ws: WebSocket, api_key: str) -> dict | None:
    if not api_key or len(api_key) < 32:
        return None
    client = await get_client_cached(api_key)
    if not client or not client["active"]:
        return None
    real_ip = ws.client.host if ws.client else "unknown"
    if not ip_allowed(real_ip, client.get("allowed_ips")):
        log.warning("[stream] IP no autorizada cliente=%s ip=%s", client["id"], real_ip)
        return None
    if not await check_and_increment_limit(client["id"], client["daily_limit"]):
        return None
    return client


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _decide_and_send(
    ws: WebSocket,
    detector,
    session_id: str,
    t0: float,
    provider: str = "groq",
    precomputed_transcript: str | None = None,
    extra_human: set[str] | None = None,
    extra_voicemail: set[str] | None = None,
    aggressive: bool = False,
) -> tuple[str, int, str]:
    """Calcula resultado final: energía + transcripción según provider del cliente."""
    energy_result = detector.on_silence() or "UNKNOWN"
    pcm = detector.audio_buffer()
    used_provider = provider or "groq"
    if precomputed_transcript is not None:
        transcript = precomputed_transcript
    elif provider == "deepgram":
        transcript = await _transcribe_deepgram(pcm, session_id)
    elif provider == "fireworks":
        transcript = await _transcribe_fireworks(pcm, session_id)
    elif provider == "openai":
        transcript = await _transcribe_openai(pcm, session_id)
    elif provider == "together":
        transcript = await _transcribe_together(pcm, session_id)
    elif provider in ("vosk", "vosk_stream"):
        transcript = await transcribe_vosk(pcm, session_id)
    elif provider == "sherpa":
        transcript = await transcribe_sherpa(pcm, session_id)
    else:
        transcript = await _transcribe_groq(pcm, session_id)

    # Fallback a Groq si el proveedor cloud devolvió None (error de servicio)
    if transcript is None:
        groq_keys = await _get_groq_keys()
        if groq_keys and provider != "groq":
            log.warning("[%s] fallback %s → groq", session_id, provider)
            transcript   = await _transcribe_groq(pcm, session_id)
            used_provider = "groq"
        else:
            transcript = ""

    # Registrar estadística del proveedor que realmente respondió
    _audio_ms = len(pcm) * 1000 // 16000
    _key_idx: int = 0
    _stat_keys: list[dict] = []
    if used_provider == "groq":
        _stat_keys = await _get_groq_keys()
    elif used_provider in ("deepgram", "deepgramv2"):
        _stat_keys = await _get_deepgram_keys()
    elif used_provider == "fireworks":
        _stat_keys = await _get_fireworks_keys()
    elif used_provider == "together":
        _stat_keys = await _get_together_keys()
    elif used_provider == "openai":
        _stat_keys = await _get_openai_keys()
    if _stat_keys:
        _key_idx = _stat_keys[_pick_key_index(session_id, _stat_keys)]["id"]
    asyncio.create_task(_record_stat(used_provider, _key_idx, _audio_ms, error=not bool(transcript)))

    if transcript:
        log.info("[%s] [%s] transcript: '%s'", session_id, provider, transcript)
        classified = _classify_transcript(transcript, extra_human=extra_human, extra_voicemail=extra_voicemail, aggressive=aggressive)
        result     = classified if classified != "UNKNOWN" else energy_result
        layer      = 2 if classified != "UNKNOWN" else 1
    else:
        # Sin palabras detectadas: si energía decía HUMAN no podemos confirmarlo
        # → UNKNOWN (no conectar agente sin evidencia de voz humana)
        result = "UNKNOWN" if energy_result == "HUMAN" else energy_result
        layer  = 1

    latency_ms = int((time.monotonic() - t0) * 1000)
    log.info("[%s] → %s layer=%d transcript='%s' %dms",
             session_id, result, layer, transcript, latency_ms)

    try:
        await ws.send_text(json.dumps({
            "status":     result,
            "transcript": transcript,
            "latency_ms": latency_ms,
            "layer":      layer,
        }))
    except Exception:
        pass

    return result, layer, transcript


# ── Handshake (recepción del JSON de auth con timeout) ───────────────────────

async def _stream_handshake(ws: WebSocket, session_id: str) -> dict | None:
    """Recibe y decodifica el primer mensaje JSON (auth) con timeout de 5s.
    Devuelve `meta`, o None si ya se cerró el WS (timeout/error/disconnect)."""
    try:
        return json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5.0))
    except Exception:
        # Si el cliente ya se desconectó abruptamente (WebSocketDisconnect,
        # code 1006) antes de mandar el JSON de auth, la conexión ya no
        # existe — intentar cerrarla de nuevo tira una segunda excepción
        # ("Unexpected ASGI message 'websocket.close'... response already
        # completed") que antes quedaba sin atrapar, mostrando un traceback
        # completo en los logs sin aportar nada nuevo.
        try:
            await ws.close(code=1008, reason="auth timeout")
        except Exception:
            pass
        return None


# ── Auth + setup de proveedor (Vosk streaming / Deepgram v2) ─────────────────

async def _stream_authenticate_and_setup(
    ws: WebSocket, session_id: str, meta: dict,
) -> tuple[dict, str, object | None, "_DeepgramStreamSession | None", set[str], set[str]] | None:
    """Autentica al cliente y prepara el proveedor de capa 2 según su modo
    (Vosk streaming abre el KaldiRecognizer, Deepgram v2 abre el WS remoto).
    Devuelve (client, provider, vosk_stream, dg_stream, ckw_human, ckw_voicemail),
    o None si no autenticó (ya cerró el WS)."""
    api_key = meta.get("api_key", "")
    client  = await _auth(ws, api_key)
    if not client:
        await ws.send_text(json.dumps({"ok": False, "error": "unauthorized"}))
        await ws.close(code=1008)
        return None

    await ws.send_text(json.dumps({"ok": True, "session_id": session_id}))
    provider      = client.get("provider", "groq")
    client_id     = client["id"]
    keywords_mode = client.get("keywords_mode", "global")
    if keywords_mode == "custom":
        ckw_human, ckw_voicemail = await get_cached_client_keywords(client_id)
    else:
        ckw_human, ckw_voicemail = set(), set()
    log.info("[%s] autenticado — cliente=%s provider=%s kw=%s",
             session_id, client["name"], provider, keywords_mode)

    # ── Vosk streaming: KaldiRecognizer abierto desde el inicio ─────────────
    vosk_stream = None
    if provider == "vosk_stream":
        extra_kw = (ckw_human | ckw_voicemail) if keywords_mode == "custom" else None
        vosk_stream = make_vosk_stream_session(_build_vosk_grammar(extra_words=extra_kw))
        if not vosk_stream:
            log.warning("[%s] Vosk streaming no disponible — usando batch", session_id)
            provider = "vosk"

    # ── Deepgram v2: sesión streaming abierta desde el inicio ────────────────
    dg_stream: _DeepgramStreamSession | None = None
    if provider == "deepgramv2":
        keys = await _get_deepgram_keys()
        if keys:
            pos   = _pick_key_index(session_id, keys)
            idx, key = keys[pos]["id"], keys[pos]["key"]
            model = await get_provider_model("deepgramv2", idx)
            try:
                dg_stream = _DeepgramStreamSession(key, session_id, model)
                await dg_stream.connect()
                log.info("[%s] Deepgram v2 streaming conectado key[%d] model=%s", session_id, idx, model)
            except Exception as e:
                log.warning("[%s] Deepgram v2 connect failed: %s", session_id, e)
                dg_stream = None

    return client, provider, vosk_stream, dg_stream, ckw_human, ckw_voicemail


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/amd/stream")
async def amd_stream(ws: WebSocket):
    session_id = secrets.token_hex(4)
    await ws.accept()
    t0 = time.monotonic()

    log.info("[%s] conexión desde %s", session_id, ws.client.host if ws.client else "?")

    meta = await _stream_handshake(ws, session_id)
    if meta is None:
        return

    call_id     = meta.get("call_id",     "")[:100]
    caller_id   = meta.get("caller_id",   "")[:50]
    lead_id     = meta.get("lead_id",     "")[:50]
    campaign_id = meta.get("campaign_id", "")[:50]
    list_id     = meta.get("list_id",     "")[:50]
    param1      = lead_id  # lead_id en param1 para búsqueda en panel

    setup = await _stream_authenticate_and_setup(ws, session_id, meta)
    if setup is None:
        return
    client, provider, vosk_stream, dg_stream, ckw_human, ckw_voicemail = setup

    # ── Loop principal ───────────────────────────────────────────────────────
    vad_engine = _cached_vad_engine or await _get_vad_engine()
    # Antes esto ignoraba vad_engine y siempre usaba StreamDetector — el
    # selector "stream vs silero" del panel admin guardaba el valor pero no
    # tenía ningún efecto real. Corregido: make_detector() sí respeta el
    # valor guardado, y cae solo a StreamDetector si Silero no está cargado
    # (fallback seguro, ver app/core/silero_vad.py:make_detector).
    # Benchmark real del modelo (2026-07-01, mismo modelo que descarga
    # deploy.sh): ~0.07ms por llamada — a la densidad de conexiones por
    # worker que deja el autotuneo de recursos (deploy.sh), muy por debajo
    # del presupuesto de 32ms por frame.
    detector = make_detector(vad_engine)
    log.debug("[%s] VAD engine: %s", session_id, vad_engine)
    result      = "UNKNOWN"
    layer       = 1
    transcript  = ""
    audio_bytes = 0
    MAX_SECS    = 8.0   # límite duro: AGI timeout=6s + margen

    async def _do_decide() -> tuple[str, int, str]:
        nonlocal dg_stream, vosk_stream
        precomp = None
        if dg_stream:
            precomp   = await dg_stream.finish()
            dg_stream = None
        elif vosk_stream:
            precomp     = vosk_stream.final()
            vosk_stream = None
        return await _decide_and_send(
            ws, detector, session_id, t0, provider, precomp,
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

            silence_limit = 1.5 if audio_bytes > 0 else 6.5
            remaining     = MAX_SECS - elapsed
            timeout       = min(silence_limit, remaining)

            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
            except asyncio.TimeoutError:
                if audio_bytes == 0:
                    # Sin audio: EAGI no proveyó datos bajo alta carga.
                    # ERROR → dialplan lo enruta a agente (no perder la llamada).
                    result, layer, transcript = "ERROR", 1, ""
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    log.warning("[%s] → ERROR (sin audio) %dms — EAGI sin datos", session_id, latency_ms)
                    try:
                        await ws.send_text(json.dumps({
                            "status": "ERROR", "transcript": "",
                            "latency_ms": latency_ms, "layer": 1,
                        }))
                    except Exception:
                        pass
                else:
                    result, layer, transcript = await _do_decide()
                break

            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()

            chunk = msg.get("bytes")
            if chunk is None:
                # Frame de texto inesperado después del auth — ignorar
                log.warning("[%s] frame texto inesperado: %r", session_id, msg.get("text","")[:60])
                continue
            if not chunk:
                continue

            prev = audio_bytes
            audio_bytes += len(chunk)
            if prev == 0:
                log.info("[%s] primer chunk %dB t=%.2fs", session_id, len(chunk), time.monotonic()-t0)
            if dg_stream:
                await dg_stream.send(chunk)
            if vosk_stream:
                vosk_stream.feed(chunk)

            # StreamDetector: puro numpy, microsegundos — no bloquea el event loop
            decision = detector.feed(chunk)
            if decision:
                result, layer, transcript = await _do_decide()
                break

    except WebSocketDisconnect:
        if audio_bytes > 0:
            # AGI terminó de enviar audio y cerró — procesar lo acumulado
            result, layer, transcript = await _do_decide()
        else:
            result, layer, transcript = "UNKNOWN", 1, ""
        log.info("[%s] desconectado audio=%dB → %s", session_id, audio_bytes, result)
    except Exception as e:
        log.error("[%s] error: %s", session_id, e)
    finally:
        if dg_stream:
            await dg_stream.cancel()
        try:
            await ws.close()
        except Exception:
            pass

    latency_ms = int((time.monotonic() - t0) * 1000)
    if transcript:
        _transcript = transcript[:200]
    elif audio_bytes == 0:
        _transcript = "[sin audio]"
    else:
        _transcript = "[silencio]"

    asyncio.create_task(save_log(
        client_id  = client["id"],
        call_id    = call_id,
        caller_id  = caller_id,
        result     = result,
        layer      = layer,
        mode       = "stream",
        latency_ms = latency_ms,
        audio_secs = round(audio_bytes / (8000 * 2), 2),
        provider   = provider if layer == 2 else "",
        transcript = _transcript,
        param1     = lead_id,
        param2     = session_id,
        param4     = f"{campaign_id}|{list_id}" if campaign_id else list_id,
        beep_detected = detector.tone_detected(),
    ))
