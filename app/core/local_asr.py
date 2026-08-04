"""
local_asr.py — Transcripción local sin red: Vosk y Sherpa-onnx.

Patrón padre/hijo, en dos niveles:
  Proceso → modelo (vosk.Model / sherpa_onnx.OfflineRecognizer) cargado UNA
            sola vez en app/main.py, a nivel de módulo, ANTES del fork de
            gunicorn (--preload) — todos los workers heredan las mismas
            páginas de memoria por copy-on-write en vez de cargar su propia
            copia cada uno (~1.4GB el modelo Vosk español × N workers si no
            se compartiera).
  Llamada → reconocedor liviano (KaldiRecognizer / stream) creado y
            destruido por cada detección AMD, reusando el modelo del proceso.

CPU-only: no requiere GPU. Funciona en EPYC/Xeon con múltiples cores.
"""
import asyncio
import json
import logging
import os

log = logging.getLogger("voxidet.local_asr")


# ── Vosk ─────────────────────────────────────────────────────────────────────

class VoskASR:
    """
    vosk.Model  (padre) — carga pesada, compartida, inmutable.
    KaldiRecognizer (hijo) — liviano, uno por llamada, destruido al terminar.
    """

    def __init__(self, model_path: str):
        try:
            import vosk as _vosk
        except ImportError:
            raise RuntimeError("vosk no instalado — pip install vosk")
        if not os.path.isdir(model_path):
            raise FileNotFoundError(f"Modelo Vosk no encontrado: {model_path}")
        _vosk.SetLogLevel(-1)
        log.info("Cargando modelo Vosk: %s ...", model_path)
        self._model = _vosk.Model(model_path)   # padre
        self._vosk  = _vosk
        self._grammar_ok = self._probe_grammar()
        if self._grammar_ok:
            log.info("Vosk listo (con soporte de gramática)")
        else:
            log.info("Vosk listo (sin soporte de gramática — modo libre)")

    def _probe_grammar(self) -> bool:
        """Detecta si el modelo soporta gramática probando una vez al startup."""
        r, w = os.pipe()
        old_stderr = os.dup(2)
        os.dup2(w, 2)
        os.close(w)
        try:
            self._vosk.KaldiRecognizer(self._model, 8000, '["hola","[unk]"]')
        finally:
            os.dup2(old_stderr, 2)
            os.close(old_stderr)
        with os.fdopen(r, "r") as f:
            output = f.read()
        return "Runtime graphs are not supported" not in output

    def transcribe_sync(self, pcm: bytes, grammar: str | None = None) -> str:
        """
        KaldiRecognizer hijo — creado y destruido por llamada.
        grammar: JSON string con lista de palabras permitidas.
                 Si se pasa, Vosk solo puede devolver esas palabras → mucha más precisión
                 para vocabulario limitado (AMD solo necesita saludos + frases de buzón).
        """
        if grammar and self._grammar_ok:
            rec = self._vosk.KaldiRecognizer(self._model, 8000, grammar)
        else:
            rec = self._vosk.KaldiRecognizer(self._model, 8000)
        rec.SetWords(False)
        chunk = 4000
        for i in range(0, len(pcm), chunk):
            rec.AcceptWaveform(pcm[i:i + chunk])
        return json.loads(rec.FinalResult()).get("text", "").strip()


# ── Sherpa-onnx ──────────────────────────────────────────────────────────────

class SherpaASR:
    """
    sherpa_onnx.OfflineRecognizer (padre) — carga pesada, compartida.
    Stream (hijo) — liviano, uno por llamada.
    Modelo: Whisper large-v3-turbo cuantizado int8 (~570 MB en disco).
    """

    def __init__(self, model_dir: str):
        try:
            import sherpa_onnx as _sherpa
            import numpy as _np
        except ImportError:
            raise RuntimeError("sherpa-onnx no instalado — pip install sherpa-onnx")
        import glob
        if not os.path.isdir(model_dir):
            raise FileNotFoundError(f"Directorio Sherpa no encontrado: {model_dir}")

        encoders = sorted(glob.glob(os.path.join(model_dir, "*encoder*.onnx")))
        decoders = sorted(glob.glob(os.path.join(model_dir, "*decoder*.onnx")))
        tokens   = (sorted(glob.glob(os.path.join(model_dir, "*tokens*.txt"))) +
                    sorted(glob.glob(os.path.join(model_dir, "tokens.txt"))))

        if not encoders or not decoders or not tokens:
            raise FileNotFoundError(
                f"Faltan archivos en {model_dir} — necesarios:\n"
                "  *encoder*.onnx, *decoder*.onnx, *tokens*.txt\n"
                "Descarga con: sudo bash scripts/download_models.sh"
            )

        log.info("Cargando modelo Sherpa-onnx: %s ...", os.path.basename(encoders[0]))
        self._recognizer = _sherpa.OfflineRecognizer.from_whisper(
            encoder=encoders[0],
            decoder=decoders[0],
            tokens=tokens[0],
            language="es",
            num_threads=4,
            provider="cpu",
            decoding_method="greedy_search",
        )
        self._np = _np
        log.info("Sherpa-onnx listo: %s", os.path.basename(encoders[0]))

    def transcribe_sync(self, pcm: bytes) -> str:
        """Stream hijo — creado y destruido por llamada."""
        stream  = self._recognizer.create_stream()
        samples = self._np.frombuffer(pcm, dtype=self._np.int16).astype(self._np.float32) / 32768.0
        stream.accept_waveform(sample_rate=8000, waveform=samples)
        self._recognizer.decode_stream(stream)
        # El resultado vive en el stream (`stream.result.text`), no en el
        # recognizer — `OfflineRecognizer` nunca tuvo un método get_result();
        # esto tiraba AttributeError en el 100% de las llamadas a Sherpa,
        # cayendo siempre al siguiente proveedor del fallback después de
        # pagar el costo completo (CPU) del decode que sí había corrido bien.
        return stream.result.text.strip()


# ── Singletons — uno por worker, iniciados en lifespan ───────────────────────

_vosk_asr:         VoskASR   | None = None
_sherpa_asr:       SherpaASR | None = None
_sherpa_large_asr: SherpaASR | None = None   # whisper-large-v3 completo (opt-in, v1.19.0)

# Semáforos para evitar sobre-suscripción de CPU en ráfagas de transcripción
# simultánea (v1.26.0) — sin esto, una ráfaga de N llamadas concurrentes
# lanzaba N transcripciones locales a la vez en el ThreadPoolExecutor default
# de asyncio, sin ningún límite relacionado a los cores reales del host.
# Sherpa crea su OfflineRecognizer con num_threads=4 fijo (ver SherpaASR.__init__)
# — cada transcripción ya consume 4 threads de CPU por sí sola, así que el
# límite de concurrencia se calcula dividiendo por 4 para no exceder los cores
# disponibles. Vosk no fija threads explícitos y es más liviano por llamada,
# así que su semáforo permite más concurrencia. Comparten `_sherpa_asr` y
# `_sherpa_large_asr` el mismo semáforo — compiten por el mismo presupuesto de
# CPU del host, no tiene sentido sumarles cupos independientes.
_SHERPA_CONCURRENCY = max(1, (os.cpu_count() or 4) // 4)
_VOSK_CONCURRENCY   = max(2, os.cpu_count() or 4)
_sherpa_semaphore = asyncio.Semaphore(_SHERPA_CONCURRENCY)
_vosk_semaphore   = asyncio.Semaphore(_VOSK_CONCURRENCY)


def init_vosk(model_path: str) -> None:
    global _vosk_asr
    if not model_path:
        return
    try:
        _vosk_asr = VoskASR(model_path)
    except Exception as e:
        log.warning("Vosk no disponible: %s", e)


def init_sherpa(model_dir: str) -> None:
    global _sherpa_asr
    if not model_dir:
        return
    try:
        _sherpa_asr = SherpaASR(model_dir)
    except Exception as e:
        log.warning("Sherpa-onnx no disponible: %s", e)


def init_sherpa_large(model_dir: str) -> None:
    """Whisper large-v3 completo — proveedor separado y opt-in (deploy.sh solo
    lo descarga si INSTALL_SHERPA_LARGE=true en credentials.conf). Mucho más
    lento en CPU que layer2_sherpa (decoder de 32 capas vs 4 de turbo) — pensado
    para que un cliente lo elija a propósito, nunca como fallback automático
    (no está en _FALLBACK_PRIORITY, ver amd_engine.py)."""
    global _sherpa_large_asr
    if not model_dir:
        return
    try:
        _sherpa_large_asr = SherpaASR(model_dir)
    except Exception as e:
        log.warning("Sherpa-onnx (large-v3) no disponible: %s", e)


def get_vosk()         -> VoskASR   | None: return _vosk_asr
def get_sherpa()       -> SherpaASR | None: return _sherpa_asr
def get_sherpa_large() -> SherpaASR | None: return _sherpa_large_asr
def vosk_loaded()         -> bool: return _vosk_asr         is not None
def sherpa_loaded()       -> bool: return _sherpa_asr       is not None
def sherpa_large_loaded() -> bool: return _sherpa_large_asr is not None


class VoskStreamSession:
    """
    KaldiRecognizer activo durante toda la llamada — procesa audio en paralelo con el VAD.
    Cuando el VAD decide, el transcript ya está casi listo: solo se llama FinalResult().
    Elimina la latencia de transcripción post-decisión que tiene el modo batch.

    feed() es synchronous y ultra-rápido (~<1ms por chunk de 20ms) — seguro en asyncio.
    """

    def __init__(self, asr: "VoskASR", grammar: str | None = None):
        if grammar and asr._grammar_ok:
            self._rec = asr._vosk.KaldiRecognizer(asr._model, 8000, grammar)
        else:
            self._rec = asr._vosk.KaldiRecognizer(asr._model, 8000)
        self._rec.SetWords(False)
        self._parts: list[str] = []

    def feed(self, chunk: bytes) -> None:
        if self._rec.AcceptWaveform(chunk):
            text = json.loads(self._rec.Result()).get("text", "").strip()
            if text:
                self._parts.append(text)

    def final(self) -> str:
        text = json.loads(self._rec.FinalResult()).get("text", "").strip()
        if text:
            self._parts.append(text)
        return " ".join(self._parts).strip()


def make_vosk_stream_session(grammar: str | None = None) -> VoskStreamSession | None:
    if not _vosk_asr:
        return None
    return VoskStreamSession(_vosk_asr, grammar)


def _read_version(ver_file: str) -> str:
    """Lee el archivo .version y devuelve su contenido, o '' si no existe."""
    try:
        with open(ver_file) as f:
            return f.read().strip()
    except OSError:
        return ""


def discover_models(base: str) -> dict[str, str]:
    """
    Descubre modelos locales leyendo archivos .version bajo base/.
    Devuelve rutas listas para init_vosk / init_sherpa / init_sherpa_large /
    init_silero_vad.
      {
        "vosk":         "/srv/models-local/vosk/vosk-model-es-0.42",  # o ""
        "sherpa":       "/srv/models-local/sherpa/...",               # o ""
        "sherpa_large": "/srv/models-local/sherpa_large/...",         # o "" (opt-in)
        "silero":       "/srv/models-local/silero/silero_vad.onnx",   # o ""
      }
    """
    paths: dict[str, str] = {"vosk": "", "sherpa": "", "sherpa_large": "", "silero": ""}

    vosk_ver = _read_version(os.path.join(base, "vosk", ".version"))
    if vosk_ver:
        candidate = os.path.join(base, "vosk", vosk_ver)
        if os.path.isdir(candidate):
            paths["vosk"] = candidate
        else:
            log.warning("Vosk .version dice '%s' pero el directorio no existe", vosk_ver)

    sherpa_ver = _read_version(os.path.join(base, "sherpa", ".version"))
    if sherpa_ver:
        candidate = os.path.join(base, "sherpa", sherpa_ver)
        if os.path.isdir(candidate):
            paths["sherpa"] = candidate
        else:
            log.warning("Sherpa .version dice '%s' pero el directorio no existe", sherpa_ver)

    sherpa_large_ver = _read_version(os.path.join(base, "sherpa_large", ".version"))
    if sherpa_large_ver:
        candidate = os.path.join(base, "sherpa_large", sherpa_large_ver)
        if os.path.isdir(candidate):
            paths["sherpa_large"] = candidate
        else:
            log.warning("Sherpa large .version dice '%s' pero el directorio no existe", sherpa_large_ver)

    silero_ver = _read_version(os.path.join(base, "silero", ".version"))
    if silero_ver:
        candidate = os.path.join(base, "silero", "silero_vad.onnx")
        if os.path.isfile(candidate):
            paths["silero"] = candidate
        else:
            log.warning("Silero .version dice '%s' pero silero_vad.onnx no existe", silero_ver)

    return paths


def get_model_versions(base: str) -> dict[str, str]:
    """Devuelve las versiones instaladas para mostrar en el panel."""
    return {
        "vosk":         _read_version(os.path.join(base, "vosk",         ".version")),
        "sherpa":       _read_version(os.path.join(base, "sherpa",       ".version")),
        "sherpa_large": _read_version(os.path.join(base, "sherpa_large", ".version")),
        "silero":       _read_version(os.path.join(base, "silero",       ".version")),
    }


_VOSK_GRAMMAR_BASE = [
    # Saludos telefónicos comunes en español latino
    "hola", "alo", "aló", "bueno", "si", "sí", "diga", "digame", "dígame",
    "hable", "adelante", "bien", "mande", "oiga", "hay", "ahi",
]


def _build_vosk_grammar(extra_words: set[str] | None = None) -> str:
    """
    Construye la gramática JSON uniendo keywords del panel + saludos base.
    extra_words: palabras adicionales del cliente (keywords_mode=custom).
    Vosk solo podrá devolver estas palabras → elimina confusiones como "el" por "hola".
    """
    from app.core import keyword_cache
    words = set(_VOSK_GRAMMAR_BASE)
    words.update(keyword_cache.get_human())
    words.update(keyword_cache.get_voicemail())
    if extra_words:
        # Solo palabras simples (sin espacios) — Vosk grammar no soporta frases
        words.update(w for w in extra_words if " " not in w)
    words.add("[unk]")
    return json.dumps(sorted(words))


async def transcribe_vosk(pcm: bytes, session_id: str) -> str:
    asr = _vosk_asr
    if not asr or len(pcm) < 1600:
        return ""
    grammar = _build_vosk_grammar()
    try:
        async with _vosk_semaphore:
            return await asyncio.get_event_loop().run_in_executor(
                None, asr.transcribe_sync, pcm, grammar
            )
    except Exception as e:
        log.warning("[%s] Vosk error: %s", session_id, e)
        return ""


async def transcribe_sherpa(pcm: bytes, session_id: str) -> str:
    asr = _sherpa_asr
    if not asr or len(pcm) < 1600:
        return ""
    try:
        async with _sherpa_semaphore:
            return await asyncio.get_event_loop().run_in_executor(
                None, asr.transcribe_sync, pcm
            )
    except Exception as e:
        log.warning("[%s] Sherpa error: %s", session_id, e)
        return ""


async def transcribe_sherpa_large(pcm: bytes, session_id: str) -> str:
    """Whisper large-v3 completo — mucho más lento que transcribe_sherpa (turbo),
    ver init_sherpa_large()."""
    asr = _sherpa_large_asr
    if not asr or len(pcm) < 1600:
        return ""
    try:
        async with _sherpa_semaphore:
            return await asyncio.get_event_loop().run_in_executor(
                None, asr.transcribe_sync, pcm
            )
    except Exception as e:
        log.warning("[%s] Sherpa (large-v3) error: %s", session_id, e)
        return ""
