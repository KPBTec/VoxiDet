"""
core/amd_engine.py — Motor de detección AMD en dos capas.

Capa 1: Análisis de energía + keywords rápidos (no API, <100ms)
Capa 2: Deepgram / Groq / OpenAI / Together / Fireworks para casos dudosos (~300ms)

Para cambiar el proveedor de IA (capa 2), solo modifica este archivo.
Única excepción a "lógica pura sin infraestructura": importa get_provider_model
(SQLAlchemy vía app.db.providers, con caché Redis) porque cada layer2_* necesita
leer el modelo configurado por key desde el panel admin — mismo mecanismo que ya
usa stream.py, sin el cual el selector de modelo del panel no tiene ningún efecto
real (ver CHANGELOG).
"""

import io
import time
import wave
import logging
import asyncio

import numpy as np
import httpx

from app.config import settings
from app.core.http_client import get_http_client
from app.core import keyword_cache
from app.core.tone_detector import ToneDetector
from app.db.providers import get_provider_model
from app.db.provider_keys import get_active_keys

log = logging.getLogger("voxidet.engine")

SILENCE_THRESHOLD = 50    # audio SIP/G.711 comprimido llega con amplitud baja
MIN_VOICE_DURATION = 0.15


# ─── Capa 1 ──────────────────────────────────────────────────────────────────

def _load_wav_samples(audio_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(audio_bytes)) as wf:
        raw = wf.readframes(wf.getnframes())
        framerate = wf.getframerate()
    return np.frombuffer(raw, dtype=np.int16), framerate


def _analyze_energy(samples: np.ndarray, framerate: int) -> dict:
    frame_size = framerate // 10
    n_frames   = len(samples) // frame_size
    energies   = [
        float(np.sqrt(np.mean(samples[i * frame_size:(i + 1) * frame_size].astype(np.float32) ** 2)))
        for i in range(n_frames)
    ]
    above     = sum(1 for e in energies if e > SILENCE_THRESHOLD)
    voice_dur = above * 0.1
    return {
        "max_energy":       max(energies) if energies else 0,
        "mean_energy":      float(np.mean(energies)) if energies else 0,
        "voice_duration":   voice_dur,
        "total_duration":   len(samples) / framerate,
        "is_mostly_silent": voice_dur < MIN_VOICE_DURATION,
    }


def layer1_detect(audio_bytes: bytes) -> tuple[str | None, dict]:
    try:
        samples, framerate = _load_wav_samples(audio_bytes)
        info = _analyze_energy(samples, framerate)

        log.info("L1 energy: max=%.0f mean=%.0f voice_dur=%.2fs",
                 info["max_energy"], info["mean_energy"], info["voice_duration"])

        if info["is_mostly_silent"]:
            return "VOICEMAIL", info
        if info["voice_duration"] < 0.8 and info["max_energy"] > 300:
            return "HUMAN", info
        if info["voice_duration"] > 2.0:
            # Voz casi todo el clip, sin pausa real → patrón típico de saludo
            # grabado/contestador. Pero un humano que contesta y arranca a
            # hablar de corrido (sin la pausa al levantar el teléfono) cae en
            # la misma zona — con más agentes conectados (más ruido/cruce en
            # el audio) esto se dispara más seguido y convertía humanos
            # reales en falsos VOICEMAIL sin que la capa 2 llegara a opinar
            # (reportado en producción, ver CHANGELOG). Solo asumir VOICEMAIL
            # de una si la voz cubre CASI TODO el clip sin ningún respiro
            # (patrón de reproducción continua real); si no, cae a capa 2
            # para que la transcripción confirme.
            if info["voice_duration"] > info["total_duration"] - 0.3:
                return "VOICEMAIL", info
            return None, info

        return None, info
    except Exception as e:
        log.error("L1 error: %s", e)
        return None, {}


# ─── Capa 2 ──────────────────────────────────────────────────────────────────

_deepgram_rr = 0  # round-robin entre keys — mismo fix que groq (ver CHANGELOG)


async def layer2_deepgram(audio_bytes: bytes, aggressive: bool = False) -> tuple[str, str]:
    global _deepgram_rr
    key_entries = await get_active_keys("deepgram")
    if not key_entries:
        log.warning("L2 deepgram: sin API key configurada → UNKNOWN")
        return "UNKNOWN", ""

    start = _deepgram_rr % len(key_entries)
    _deepgram_rr += 1
    client = get_http_client()
    for attempt in range(len(key_entries)):
        entry = key_entries[(start + attempt) % len(key_entries)]
        idx, key = entry["id"], entry["key"]
        model = await get_provider_model("deepgram", idx)
        try:
            resp = await client.post(
                "https://api.deepgram.com/v1/listen"
                f"?model={model}&language=es&punctuate=false"
                "&smart_format=false&diarize=false",
                headers={
                    "Authorization": f"Token {key}",
                    "Content-Type": "audio/wav",
                },
                content=audio_bytes,
                timeout=4.0,
            )
            if resp.status_code == 429:
                log.warning("L2 deepgram: key[%d] límite (429) — rotando", idx)
                continue
            resp.raise_for_status()
            transcript = (
                resp.json()
                .get("results", {})
                .get("channels", [{}])[0]
                .get("alternatives", [{}])[0]
                .get("transcript", "")
                .lower()
                .strip()
            )

            log.debug("L2 deepgram transcript (key[%d] model=%s): '%s'", idx, model, transcript)
            return _classify_transcript(transcript, aggressive=aggressive), transcript

        except httpx.TimeoutException:
            log.warning("L2 deepgram: timeout → UNKNOWN")
            await _mark_provider_down("deepgram")
            return "UNKNOWN", ""
        except Exception as e:
            log.error("L2 deepgram error: %s", e)
            await _mark_provider_down("deepgram")
            return "UNKNOWN", ""
    log.warning("L2 deepgram: las %d keys configuradas están al límite → UNKNOWN", len(key_entries))
    return "UNKNOWN", ""


def _wav_ok(audio_bytes: bytes) -> bool:
    return len(audio_bytes) > 44 + 1600   # header WAV (44B) + mínimo de audio útil


_groq_rr = 0  # solo el punto de partida — el cupo real lo decide _groq_claim_slot
              # (Redis, compartido entre TODOS los workers de gunicorn). Antes esto
              # rotaba round-robin pero cada worker llevaba su propio contador en
              # memoria de proceso, sin coordinarse con los demás — Groq cuenta el
              # RPM del lado de ellos sumando el tráfico de todos los workers juntos,
              # así que un worker podía creer que una key tenía cupo cuando ya la
              # habían agotado los demás (ver CHANGELOG).

_GROQ_FREE_RPM = 20  # límite del plan free de Groq por key (mismo valor que stream.py)


async def _groq_claim_slot(idx: int) -> bool:
    """Reserva un cupo de esta key en Redis — mismo mecanismo que stream.py
    (_groq_claim_slot), portado a batch. Devuelve True si aún hay cupo en la
    ventana de 60s, compartido entre todos los workers."""
    try:
        from app.cache.client_cache import get_redis
        r = await get_redis()
        rkey = f"groq:rpm:{idx}"
        count = await r.incr(rkey)
        if count == 1:
            await r.expire(rkey, 60)
        return count <= _GROQ_FREE_RPM
    except Exception:
        return True  # si Redis falla, dejar pasar (fail-open, igual que stream.py)


async def layer2_groq(audio_bytes: bytes, aggressive: bool = False) -> tuple[str, str]:
    global _groq_rr
    key_entries = await get_active_keys("groq")
    if not key_entries:
        log.warning("L2 groq: sin API key configurada → UNKNOWN")
        return "UNKNOWN", ""
    if not _wav_ok(audio_bytes):
        log.debug("L2 groq: audio muy corto para transcribir → UNKNOWN")
        return "UNKNOWN", ""

    start = _groq_rr % len(key_entries)
    _groq_rr += 1
    client = get_http_client()
    for attempt in range(len(key_entries)):
        entry = key_entries[(start + attempt) % len(key_entries)]
        idx, key = entry["id"], entry["key"]
        if not await _groq_claim_slot(idx):
            log.info("L2 groq: key[%d] sin cupo RPM (Redis, ya reservado por otro worker) — rotando sin llamar", idx)
            continue
        model = await get_provider_model("groq", idx)
        try:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", audio_bytes, "audio/wav")},
                data={"model": model, "language": "es", "response_format": "json"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                transcript = resp.json().get("text", "").strip().lower()
                log.debug("L2 groq transcript (key[%d] model=%s): '%s'", idx, model, transcript)
                return _classify_transcript(transcript, aggressive=aggressive), transcript
            if resp.status_code == 429:
                # Redis decía que había cupo pero Groq igual rechazó — forzar el
                # contador al límite para que los DEMÁS workers también sepan que
                # esta key está agotada de una, en vez de que cada uno lo descubra
                # a los golpes por su cuenta (mismo patrón que stream.py).
                try:
                    from app.cache.client_cache import get_redis
                    r = await get_redis()
                    await r.set(f"groq:rpm:{idx}", _GROQ_FREE_RPM + 1, ex=60)
                except Exception:
                    pass
                log.warning("L2 groq: key[%d] 429 inesperado — contador Redis sincronizado", idx)
                continue
            log.warning("L2 groq: %s — %s", resp.status_code, resp.text[:200])
            await _mark_provider_down("groq")
            return "UNKNOWN", ""
        except httpx.TimeoutException:
            log.warning("L2 groq: timeout → UNKNOWN")
            await _mark_provider_down("groq")
            return "UNKNOWN", ""
        except Exception as e:
            log.error("L2 groq error: %s", e)
            await _mark_provider_down("groq")
            return "UNKNOWN", ""
    log.warning("L2 groq: las %d keys configuradas están al límite RPM → UNKNOWN", len(key_entries))
    return "UNKNOWN", ""


_openai_rr = 0  # round-robin entre keys — mismo fix que groq (ver CHANGELOG)


async def layer2_openai(audio_bytes: bytes, aggressive: bool = False) -> tuple[str, str]:
    global _openai_rr
    key_entries = await get_active_keys("openai")
    if not key_entries:
        log.warning("L2 openai: sin API key configurada → UNKNOWN")
        return "UNKNOWN", ""
    if not _wav_ok(audio_bytes):
        log.debug("L2 openai: audio muy corto para transcribir → UNKNOWN")
        return "UNKNOWN", ""

    start = _openai_rr % len(key_entries)
    _openai_rr += 1
    client = get_http_client()
    for attempt in range(len(key_entries)):
        entry = key_entries[(start + attempt) % len(key_entries)]
        idx, key = entry["id"], entry["key"]
        model = await get_provider_model("openai", idx)
        try:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", audio_bytes, "audio/wav")},
                data={"model": model, "language": "es", "response_format": "json"},
                timeout=8.0,
            )
            if resp.status_code == 200:
                transcript = resp.json().get("text", "").strip().lower()
                log.debug("L2 openai transcript (key[%d] model=%s): '%s'", idx, model, transcript)
                return _classify_transcript(transcript, aggressive=aggressive), transcript
            if resp.status_code == 429:
                log.warning("L2 openai: key[%d] límite (429) — rotando", idx)
                continue
            log.warning("L2 openai: %s — %s", resp.status_code, resp.text[:200])
            await _mark_provider_down("openai")
            return "UNKNOWN", ""
        except httpx.TimeoutException:
            log.warning("L2 openai: timeout → UNKNOWN")
            await _mark_provider_down("openai")
            return "UNKNOWN", ""
        except Exception as e:
            log.error("L2 openai error: %s", e)
            await _mark_provider_down("openai")
            return "UNKNOWN", ""
    log.warning("L2 openai: las %d keys configuradas están al límite → UNKNOWN", len(key_entries))
    return "UNKNOWN", ""


_together_rr = 0  # round-robin entre keys — mismo fix que groq (ver CHANGELOG)


async def layer2_together(audio_bytes: bytes, aggressive: bool = False) -> tuple[str, str]:
    global _together_rr
    key_entries = await get_active_keys("together")
    if not key_entries:
        log.warning("L2 together: sin API key configurada → UNKNOWN")
        return "UNKNOWN", ""
    if not _wav_ok(audio_bytes):
        log.debug("L2 together: audio muy corto para transcribir → UNKNOWN")
        return "UNKNOWN", ""

    start = _together_rr % len(key_entries)
    _together_rr += 1
    client = get_http_client()
    for attempt in range(len(key_entries)):
        entry = key_entries[(start + attempt) % len(key_entries)]
        idx, key = entry["id"], entry["key"]
        model = await get_provider_model("together", idx)
        try:
            resp = await client.post(
                "https://api.together.xyz/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", audio_bytes, "audio/wav")},
                data={"model": model, "language": "es", "response_format": "json"},
                timeout=8.0,
            )
            if resp.status_code == 200:
                transcript = resp.json().get("text", "").strip().lower()
                log.debug("L2 together transcript (key[%d] model=%s): '%s'", idx, model, transcript)
                return _classify_transcript(transcript, aggressive=aggressive), transcript
            if resp.status_code == 429:
                log.warning("L2 together: key[%d] límite (429) — rotando", idx)
                continue
            log.warning("L2 together: %s — %s", resp.status_code, resp.text[:200])
            await _mark_provider_down("together")
            return "UNKNOWN", ""
        except httpx.TimeoutException:
            log.warning("L2 together: timeout → UNKNOWN")
            await _mark_provider_down("together")
            return "UNKNOWN", ""
        except Exception as e:
            log.error("L2 together error: %s", e)
            await _mark_provider_down("together")
            return "UNKNOWN", ""
    log.warning("L2 together: las %d keys configuradas están al límite → UNKNOWN", len(key_entries))
    return "UNKNOWN", ""


_fireworks_rr = 0  # round-robin entre keys — mismo fix que groq (ver CHANGELOG)


async def layer2_fireworks(audio_bytes: bytes, aggressive: bool = False) -> tuple[str, str]:
    global _fireworks_rr
    key_entries = await get_active_keys("fireworks")
    if not key_entries:
        log.warning("L2 fireworks: sin API key configurada → UNKNOWN")
        return "UNKNOWN", ""
    if not _wav_ok(audio_bytes):
        log.debug("L2 fireworks: audio muy corto para transcribir → UNKNOWN")
        return "UNKNOWN", ""

    start = _fireworks_rr % len(key_entries)
    _fireworks_rr += 1
    client = get_http_client()
    for attempt in range(len(key_entries)):
        entry = key_entries[(start + attempt) % len(key_entries)]
        idx, key = entry["id"], entry["key"]
        model = await get_provider_model("fireworks", idx)
        try:
            resp = await client.post(
                "https://api.fireworks.ai/inference/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", audio_bytes, "audio/wav")},
                data={"model": model, "language": "es", "response_format": "json"},
                timeout=8.0,
            )
            if resp.status_code == 200:
                transcript = resp.json().get("text", "").strip().lower()
                log.debug("L2 fireworks transcript (key[%d] model=%s): '%s'", idx, model, transcript)
                return _classify_transcript(transcript, aggressive=aggressive), transcript
            if resp.status_code == 429:
                log.warning("L2 fireworks: key[%d] límite (429) — rotando", idx)
                continue
            log.warning("L2 fireworks: %s — %s", resp.status_code, resp.text[:200])
            await _mark_provider_down("fireworks")
            return "UNKNOWN", ""
        except httpx.TimeoutException:
            log.warning("L2 fireworks: timeout → UNKNOWN")
            await _mark_provider_down("fireworks")
            return "UNKNOWN", ""
        except Exception as e:
            log.error("L2 fireworks error: %s", e)
            await _mark_provider_down("fireworks")
            return "UNKNOWN", ""
    log.warning("L2 fireworks: las %d keys configuradas están al límite → UNKNOWN", len(key_entries))
    return "UNKNOWN", ""


async def layer2_vosk(audio_bytes: bytes, aggressive: bool = False) -> tuple[str, str]:
    """Vosk local (batch) — nunca visto en este modo hasta ahora, solo en stream.
    Gratis y sin límite RPM: primer fallback antes que proveedores cloud de pago
    cuando el proveedor del cliente falla (ver CHANGELOG)."""
    from app.core.local_asr import transcribe_vosk
    if not _wav_ok(audio_bytes):
        log.debug("L2 vosk: audio muy corto para transcribir → UNKNOWN")
        return "UNKNOWN", ""
    try:
        with wave.open(io.BytesIO(audio_bytes)) as wf:
            pcm = wf.readframes(wf.getnframes())
    except Exception as e:
        log.error("L2 vosk error leyendo WAV: %s", e)
        return "UNKNOWN", ""
    transcript = await transcribe_vosk(pcm, session_id="batch")
    if not transcript:
        # INFO (no debug): sin esto, cuando Vosk no reconoce nada y el fallback
        # cae a Groq/OpenAI, no quedaba ningún rastro de que Vosk fue el primero
        # en intentarlo — parecía que "no se estaba usando" cuando en realidad
        # sí se probó, solo que en silencio.
        log.info("L2 vosk: sin transcripción reconocible → cae al siguiente proveedor")
        return "UNKNOWN", ""
    log.debug("L2 vosk transcript: '%s'", transcript)
    return _classify_transcript(transcript, aggressive=aggressive), transcript


async def layer2_sherpa(audio_bytes: bytes, aggressive: bool = False) -> tuple[str, str]:
    """Sherpa-onnx local (batch) — mismo fix que layer2_vosk."""
    from app.core.local_asr import transcribe_sherpa
    if not _wav_ok(audio_bytes):
        log.debug("L2 sherpa: audio muy corto para transcribir → UNKNOWN")
        return "UNKNOWN", ""
    try:
        with wave.open(io.BytesIO(audio_bytes)) as wf:
            pcm = wf.readframes(wf.getnframes())
    except Exception as e:
        log.error("L2 sherpa error leyendo WAV: %s", e)
        return "UNKNOWN", ""
    transcript = await transcribe_sherpa(pcm, session_id="batch")
    if not transcript:
        log.info("L2 sherpa: sin transcripción reconocible → cae al siguiente proveedor")
        return "UNKNOWN", ""
    log.debug("L2 sherpa transcript: '%s'", transcript)
    return _classify_transcript(transcript, aggressive=aggressive), transcript


async def layer2_sherpa_large(audio_bytes: bytes, aggressive: bool = False) -> tuple[str, str]:
    """Whisper large-v3 completo (opt-in, v1.19.0) — mucho más lento que
    layer2_sherpa (turbo): decoder de 32 capas corriendo secuencial en CPU,
    varios segundos por clip corto en vez de cientos de ms. A propósito NO
    está en _FALLBACK_PRIORITY ni en _NEVER_AUTO_FALLBACK-excluido de ella —
    solo corre si un cliente lo elige como su proveedor directo, nunca como
    fallback automático de otro cliente (ver detect())."""
    from app.core.local_asr import transcribe_sherpa_large
    if not _wav_ok(audio_bytes):
        log.debug("L2 sherpa_large: audio muy corto para transcribir → UNKNOWN")
        return "UNKNOWN", ""
    try:
        with wave.open(io.BytesIO(audio_bytes)) as wf:
            pcm = wf.readframes(wf.getnframes())
    except Exception as e:
        log.error("L2 sherpa_large error leyendo WAV: %s", e)
        return "UNKNOWN", ""
    transcript = await transcribe_sherpa_large(pcm, session_id="batch")
    if not transcript:
        log.info("L2 sherpa_large: sin transcripción reconocible → UNKNOWN")
        return "UNKNOWN", ""
    log.debug("L2 sherpa_large transcript: '%s'", transcript)
    return _classify_transcript(transcript, aggressive=aggressive), transcript


# Orden de prioridad para el fallback automático cuando el proveedor del cliente
# falla (ver detect()/transcribe_for_log()): local primero — Vosk/Sherpa son
# gratis, sin límite RPM y no alucinan texto en audio ruidoso/silencioso porque
# son reconocimiento real, no generativo. Deepgram/Fireworks/Together antes que
# OpenAI porque Whisper (usado por OpenAI) tiende a inventar frases genéricas
# ("hello, world!", saludos de buzón) en vez de devolver vacío ante audio
# ambiguo — confirmado en producción, ver CHANGELOG. OpenAI queda de última
# opción, no eliminado — sigue siendo mejor que UNKNOWN si nada más responde.
# sherpa_large NUNCA entra acá (ver _NEVER_AUTO_FALLBACK) — varios segundos
# por clip en CPU, inaceptable como fallback silencioso de OTRO cliente.
_FALLBACK_PRIORITY = ["vosk", "sherpa", "deepgram", "fireworks", "together", "groq", "openai"]

# Proveedores que solo se usan si un cliente los elige directamente como su
# proveedor principal — nunca como fallback automático cuando falla el de
# OTRO cliente, sin importar si están "activos" en el panel Proveedores.
_NEVER_AUTO_FALLBACK = {"sherpa_large"}

# Circuit breaker — sin esto, cada request que cae en fallback vuelve a pagar
# el timeout completo (hasta 8s) de un proveedor que ya está caído, request
# tras request, sin ninguna memoria entre llamadas. Redis (no memoria local)
# porque el estado tiene que ser el mismo para los 11 workers de gunicorn —
# un proveedor caído lo está para todos, no solo para el worker que lo notó.
_CIRCUIT_TTL = 45  # segundos que un proveedor queda en cooldown tras un fallo real (timeout/excepción/5xx)


async def _mark_provider_down(provider: str) -> None:
    try:
        from app.cache.client_cache import get_redis
        r = await get_redis()
        await r.set(f"amd:circuit:{provider}", "1", ex=_CIRCUIT_TTL)
    except Exception:
        pass  # Redis caído no debe romper la detección — solo se pierde el circuit breaker, no la detección en sí

    from app.core.alerting import notify
    await notify(f"proveedor_caido:{provider}", f"proveedor **{provider}** entró en cooldown por un fallo real (timeout/error) — se está saltando en el fallback por {_CIRCUIT_TTL}s")


async def _get_down_providers(providers: list[str]) -> set[str]:
    if not providers:
        return set()
    try:
        from app.cache.client_cache import get_redis
        r = await get_redis()
        keys = [f"amd:circuit:{p}" for p in providers]
        vals = await r.mget(keys)
        return {p for p, v in zip(providers, vals) if v}
    except Exception:
        return set()


def _by_fallback_priority(providers: list[str]) -> list[str]:
    providers = [p for p in providers if p not in _NEVER_AUTO_FALLBACK]
    return sorted(
        providers,
        key=lambda p: _FALLBACK_PRIORITY.index(p) if p in _FALLBACK_PRIORITY else len(_FALLBACK_PRIORITY),
    )


_LAYER2_PROVIDERS = {
    "groq":          layer2_groq,
    "deepgram":      layer2_deepgram,
    "openai":        layer2_openai,
    "together":      layer2_together,
    "fireworks":     layer2_fireworks,
    "vosk":          layer2_vosk,
    "sherpa":        layer2_sherpa,
    "sherpa_large":  layer2_sherpa_large,
}


import re as _re
_URL_RE = _re.compile(
    r'(?:https?://|www\.)\S+|\S+\.(?:com|org|net|io|es|pe|info|tv|co)\b',
    _re.IGNORECASE,
)


def _has_non_latin_script(text: str) -> bool:
    """Detecta scripts no-latinos (CJK, árabe, etc.) — alucinación del modelo."""
    import unicodedata
    for ch in text:
        if ch.isalpha():
            try:
                name = unicodedata.name(ch, "")
            except Exception:
                return True
            if not any(s in name for s in ("LATIN", "COMMON")):
                return True
    return False


def _has_url(text: str) -> bool:
    """Detecta URLs en el transcript — nadie contesta el teléfono diciendo www.algo.com."""
    return bool(_URL_RE.search(text))


def _classify_transcript(
    transcript: str,
    extra_human: set[str] | None = None,
    extra_voicemail: set[str] | None = None,
    aggressive: bool = False,
) -> str:
    if not transcript:
        return "UNKNOWN"

    # Hallucinations del modelo: scripts no-latinos o URLs
    if _has_non_latin_script(transcript) or _has_url(transcript):
        return "UNKNOWN"

    t = transcript.lower()
    for ch in '.,;:!?¡¿"\'()[]{}':
        t = t.replace(ch, ' ')
    words    = t.split()
    word_set = set(words)
    n        = len(words)

    if not words:
        return "UNKNOWN"

    # Combinar keywords globales + keywords del cliente
    vm_words   = keyword_cache.get_voicemail_words()
    hm_words   = keyword_cache.get_human_words()
    vm_phrases = list(keyword_cache.get_voicemail_phrases())
    hm_phrases = list(keyword_cache.get_human_phrases())
    if extra_voicemail:
        vm_words   = vm_words | {w for w in extra_voicemail if " " not in w}
        vm_phrases = vm_phrases + [w for w in extra_voicemail if " " in w]
    if extra_human:
        hm_words   = hm_words | {w for w in extra_human if " " not in w}
        hm_phrases = hm_phrases + [w for w in extra_human if " " in w]

    # 1. Frases completas de buzón → VOICEMAIL inmediato
    for phrase in vm_phrases:
        if phrase in t:
            return "VOICEMAIL"

    # 2. Palabra individual de buzón → VOICEMAIL
    if word_set & vm_words:
        return "VOICEMAIL"

    # 3. Primera palabra es saludo → HUMAN fuerte
    if words[0] in (hm_words | (extra_human or set())):
        return "HUMAN"

    # 4. Frase corta (≤3 palabras) sin señal de buzón → HUMAN
    if n <= 3:
        return "HUMAN"

    # 5. Frase de humano multi-palabra en texto más largo
    for phrase in hm_phrases:
        if phrase in t:
            return "HUMAN"

    # 6. Keyword de humano individual en frase más larga
    if word_set & hm_words:
        return "HUMAN"

    # 7 y 8: sin ninguna señal de keyword — ambiguo por diseño. El default
    # histórico es "ante la duda, VOICEMAIL" (evita conectar al agente con un
    # contestador real, a costa de perder algún lead). Reportado en producción
    # que esta ambigüedad aumenta con más agentes conectados simultáneamente
    # (audio con más ruido/cruce, transcripciones más cortas) — eso convierte
    # humanos reales en falsos VOICEMAIL más seguido de lo esperado. Con
    # amd_bias='aggressive' (por cliente, panel Clientes) se admite la duda
    # como UNKNOWN en vez de asumir VOICEMAIL — requiere que el dialplan del
    # cliente enrute UNKNOWN al agente, si no, no cambia nada en la práctica.
    log.info("L2 zona ambigua: n=%d words aggressive=%s → %s",
              n, aggressive, "UNKNOWN" if aggressive else "VOICEMAIL")

    # 7. Frase muy larga sin ninguna keyword → IVR o buzón grabado
    if n > 10:
        return "UNKNOWN" if aggressive else "VOICEMAIL"

    # 8. Zona gris (4-10 palabras sin keywords)
    return "UNKNOWN" if aggressive else "VOICEMAIL"


# ─── Streaming (EAGI real-time) ──────────────────────────────────────────────

_FRAME_SAMPLES = 800   # 100ms at 8 kHz
_FRAME_BYTES   = _FRAME_SAMPLES * 2  # int16 → 2 bytes/sample


class StreamDetector:
    """
    Analyzes raw PCM 8 kHz / 16-bit / mono chunks streamed in real time from EAGI fd3.
    Incremental: each new chunk only computes energy for the new frames — O(1) per call.
    """

    def __init__(self):
        self._buf     = b""
        self._pending = b""    # bytes de nuevo audio no procesados aún
        self._energies: list[float] = []
        self._t0      = time.monotonic()
        # Experimental — solo logging, no participa en la decisión todavía.
        # Ver app/core/tone_detector.py.
        self._tone = ToneDetector(settings.AMD_BEEP_FREQ_HZ)
        self._tone_logged = False

    def feed(self, chunk: bytes) -> str | None:
        self._buf     += chunk
        self._pending += chunk

        # Solo computar energía para los frames NUEVOS (no todo el buffer)
        while len(self._pending) >= _FRAME_BYTES:
            frame = self._pending[:_FRAME_BYTES]
            self._pending = self._pending[_FRAME_BYTES:]
            samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
            self._energies.append(float(np.sqrt(np.mean(samples ** 2))))
            if self._tone.feed(samples) and not self._tone_logged:
                self._tone_logged = True
                log.info("Stream L1 tono de beep detectado (~%.0fHz, experimental, no decide)",
                          settings.AMD_BEEP_FREQ_HZ)

        n_frames = len(self._energies)
        if n_frames < 3:
            return None

        energies  = self._energies
        above     = sum(1 for e in energies if e > SILENCE_THRESHOLD)
        voice_dur = above * 0.1
        max_e     = max(energies)
        mean_e    = sum(energies) / n_frames
        elapsed   = time.monotonic() - self._t0

        log.info("Stream L1 n=%d voice=%.1fs max_e=%.0f mean_e=%.0f elapsed=%.1fs",
                 n_frames, voice_dur, max_e, mean_e, elapsed)

        words   = 0
        in_word = False
        sil_run = 0
        wrd_run = 0
        for e in energies:
            if e > SILENCE_THRESHOLD:
                sil_run  = 0
                wrd_run += 1
                in_word  = True
            else:
                if in_word:
                    sil_run += 1
                    if sil_run >= 5:
                        if wrd_run >= 1:
                            words += 1
                        in_word = False
                        wrd_run = 0

        if words >= 3:
            return "VOICEMAIL"

        if words >= 1 and not in_word and sil_run >= 5 and elapsed >= 1.0:
            return "HUMAN"

        if voice_dur > 2.0:
            return "VOICEMAIL"

        if elapsed > 1.5 and voice_dur < 0.1:
            return "VOICEMAIL"

        if elapsed > 4.5:
            if 0.1 <= voice_dur <= 1.5:
                return "HUMAN"
            return "VOICEMAIL"

        return None

    def audio_buffer(self) -> bytes:
        return self._buf

    def tone_detected(self) -> bool:
        """True si se confirmó un tono sostenido (ver ToneDetector) — experimental,
        para guardar en logs y calibrar, no participa en la decisión todavía."""
        return self._tone.confirmed

    def on_silence(self) -> str | None:
        elapsed  = time.monotonic() - self._t0
        energies = self._energies
        if not energies:
            return "VOICEMAIL" if elapsed > 1.0 else None

        above     = sum(1 for e in energies if e > SILENCE_THRESHOLD)
        voice_dur = above * 0.1

        log.info("on_silence: voice=%.1fs n=%d elapsed=%.1fs", voice_dur, len(energies), elapsed)

        if 0.2 <= voice_dur <= 1.2:
            return "HUMAN"
        if voice_dur > 1.2:
            return "VOICEMAIL"
        return "VOICEMAIL"


# ─── Entry point ─────────────────────────────────────────────────────────────

async def detect(
    audio_bytes: bytes,
    provider: str = "groq",
    active_providers: list[str] | None = None,
    aggressive: bool = False,
) -> dict:
    """
    provider: proveedor configurado por el cliente (panel admin) — se intenta
    primero, sea cual sea (mismo criterio que el modo stream, que ya lo
    respeta desde siempre).
    active_providers: proveedores habilitados globalmente (panel Proveedores)
    — si el proveedor del cliente falla, el fallback SOLO prueba proveedores
    de esta lista, nunca uno deshabilitado.
    aggressive: viene de clients.amd_bias == 'aggressive' — en transcripciones
    ambiguas de capa 2, devuelve UNKNOWN en vez de asumir VOICEMAIL (ver
    _classify_transcript). Default False = comportamiento histórico.
    """
    t0 = time.monotonic()

    result, energy_info = layer1_detect(audio_bytes)
    layer = 1

    transcript     = ""
    used_provider  = ""
    if result is None:
        layer = 2
        # Providers activos solo se resuelven acá — capa 1 (energía, sin red
        # ni DB) ya devolvió None antes de este punto, así que recién ahora
        # hace falta el dato. Antes se resolvía siempre por adelantado en el
        # caller (app/api/amd.py), atando el ~70% de las llamadas que capa 1
        # resuelve sola a una dependencia de MySQL que no necesitaban.
        if active_providers is None:
            from app.db.providers import get_active_providers
            active_providers = await get_active_providers()
        candidates = [provider] + _by_fallback_priority([p for p in active_providers if p != provider])
        # Saltar proveedores marcados como caídos recientemente (circuit
        # breaker, ver _mark_provider_down) — si TODOS están en cooldown, se
        # prueba la lista completa igual (mejor un intento con timeout
        # completo que garantizar UNKNOWN sin siquiera intentarlo).
        down = await _get_down_providers(candidates)
        if down:
            filtered = [p for p in candidates if p not in down]
            candidates = filtered or candidates
        for p in candidates:
            func = _LAYER2_PROVIDERS.get(p)
            if not func:
                continue
            used_provider = p
            result, transcript = await func(audio_bytes, aggressive=aggressive)
            if result and result != "UNKNOWN":
                break
        if not result:
            result = "UNKNOWN"

    return {
        "result":      result or "UNKNOWN",
        "layer_used":  layer,
        "latency_ms":  int((time.monotonic() - t0) * 1000),
        "transcript":  transcript,
        "energy_info": energy_info,
        "provider":    used_provider,
    }


async def transcribe_for_log(
    audio_bytes: bytes,
    provider: str = "groq",
    active_providers: list[str] | None = None,
) -> tuple[str, str]:
    """
    Transcribe SIEMPRE, aunque la capa 1 ya haya decidido — pensada para
    correr en segundo plano (no bloquea la respuesta de AMDSTATUS a
    Asterisk) solo para dejar el transcript disponible en el log, que sirve
    como registro/auditoría de cada llamada. Mismo fallback que detect():
    proveedor del cliente primero, y si falla, otro proveedor activo.
    Devuelve (provider_usado, transcript) — nunca cambia la decisión de AMD.
    """
    if active_providers is None:
        from app.db.providers import get_active_providers
        active_providers = await get_active_providers()
    candidates = [provider] + _by_fallback_priority([p for p in active_providers if p != provider])
    for p in candidates:
        func = _LAYER2_PROVIDERS.get(p)
        if not func:
            continue
        _, transcript = await func(audio_bytes)
        if transcript:
            return p, transcript
    return "", ""
