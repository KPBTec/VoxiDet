"""
silero_vad.py — VAD neuronal con modelo Silero ONNX.

Dos clases:
  SileroVAD             — singleton que carga el modelo .onnx una vez
  SileroStreamDetector  — misma interfaz que StreamDetector, usa Silero internamente

El modelo (~1.8 MB) es una LSTM entrenada en millones de horas de audio telefónico.
Salida: probabilidad de voz 0-1 por cada 32 ms de audio (256 samples a 8 kHz).
"""
import logging
import time

import numpy as np

from app.config import settings
from app.core.tone_detector import ToneDetector

log = logging.getLogger("voxidet.silero_vad")

_FRAME_SAMPLES = 256        # 32 ms a 8 kHz (requerido por Silero)
_FRAME_BYTES   = _FRAME_SAMPLES * 2
_THRESHOLD     = 0.5        # prob ≥ 0.5 → voz
_SIL_WORD_END  = 16         # 16 frames × 32ms = 512ms de silencio cierra palabra
_SIL_HUMAN     = 16         # 512ms silencio post-palabra → HUMAN
_ELAPSED_HUMAN = 1.0        # mínimo tiempo antes de decidir HUMAN (da margen para "hola hola")


# ── Modelo ONNX ───────────────────────────────────────────────────────────────

class SileroVAD:
    """
    Carga el modelo Silero ONNX una vez. Soporta v4 y v5 automáticamente.
      v4: inputs  input, h [2,1,64], c [2,1,64], sr
      v5: inputs  input, state [2,1,128], sr         ← modelo actual en GitHub
    """

    def __init__(self, model_path: str):
        try:
            import onnxruntime as ort
        except ImportError:
            raise RuntimeError("onnxruntime no instalado — pip install onnxruntime")
        log.info("Cargando Silero VAD: %s", model_path)
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level   = 3
        self._session = ort.InferenceSession(model_path, sess_options=opts)
        # Detectar versión por los nombres de entrada del modelo
        input_names   = {inp.name for inp in self._session.get_inputs()}
        self._v5      = "state" in input_names
        log.info("Silero VAD listo (API %s)", "v5" if self._v5 else "v4")

    def new_state(self) -> np.ndarray:
        """Estado inicial — un tensor unificado compatible con v4 y v5."""
        if self._v5:
            return np.zeros((2, 1, 128), dtype=np.float32)   # v5: state [2,1,128]
        return np.zeros((2, 2, 64), dtype=np.float32)         # v4: h+c apilados

    def predict(self, samples: np.ndarray, state: np.ndarray):
        """
        samples: float32 normalizado [-1, 1], exactamente 256 muestras.
        state:   resultado de new_state() o la salida anterior de predict().
        Devuelve (probabilidad_voz, state_nuevo).
        """
        x = samples[np.newaxis, :]   # [1, 256]
        if self._v5:
            out, state_new = self._session.run(
                None,
                {"input": x, "state": state, "sr": np.array(8000, dtype=np.int64)},
            )
        else:
            # v4: separar h y c del tensor apilado
            h, c = state[0:1], state[1:2]
            out, h_new, c_new = self._session.run(
                None,
                {"input": x, "h": h, "c": c, "sr": np.array(8000, dtype=np.int64)},
            )
            state_new = np.concatenate([h_new, c_new], axis=0)
        return float(np.squeeze(out)), state_new


# ── Detector con misma interfaz que StreamDetector ────────────────────────────

class SileroStreamDetector:
    """
    Reemplaza StreamDetector usando Silero VAD en vez de umbrales de energía.
    Interfaz idéntica: feed(chunk) → str|None, on_silence() → str|None,
    audio_buffer() → bytes.
    """

    def __init__(self, vad: SileroVAD):
        self._vad     = vad
        self._state   = vad.new_state()   # tensor unificado, compatible v4 y v5
        self._pcm     = b""    # buffer completo para transcripción
        self._pending = b""    # bytes pendientes de procesar por Silero
        self._voiced  : list[bool] = []   # True/False por frame de 32ms
        self._t0      = time.monotonic()
        # Experimental — solo logging, no participa en la decisión todavía.
        # Ver app/core/tone_detector.py.
        self._tone = ToneDetector(settings.AMD_BEEP_FREQ_HZ)
        self._tone_logged = False

    # ── Público ───────────────────────────────────────────────────────────────

    def feed(self, chunk: bytes) -> str | None:
        self._pcm     += chunk
        self._pending += chunk

        # Procesar frames completos de 256 muestras (32ms)
        while len(self._pending) >= _FRAME_BYTES:
            frame = self._pending[:_FRAME_BYTES]
            self._pending = self._pending[_FRAME_BYTES:]
            samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
            prob, self._state = self._vad.predict(samples, self._state)
            self._voiced.append(prob >= _THRESHOLD)
            if self._tone.feed(samples) and not self._tone_logged:
                self._tone_logged = True
                log.info("Silero tono de beep detectado (~%.0fHz, experimental, no decide)",
                          settings.AMD_BEEP_FREQ_HZ)

        # Necesitamos al menos 320ms antes de decidir (10 frames)
        if len(self._voiced) < 10:
            return None

        return self._classify()

    def on_silence(self) -> str | None:
        """Llamado cuando el WebSocket deja de recibir chunks (silencio real)."""
        elapsed = time.monotonic() - self._t0
        if not self._voiced:
            return "VOICEMAIL" if elapsed > 1.0 else None
        voice_dur = sum(self._voiced) * 0.032
        if 0.1 <= voice_dur <= 1.5:
            return "HUMAN"
        if voice_dur > 1.5:
            return "VOICEMAIL"
        return "VOICEMAIL"

    def audio_buffer(self) -> bytes:
        """Buffer PCM completo para el transcriptor (Vosk/Groq/etc.)."""
        return self._pcm

    def tone_detected(self) -> bool:
        """True si se confirmó un tono sostenido (ver ToneDetector) — experimental,
        para guardar en logs y calibrar, no participa en la decisión todavía."""
        return self._tone.confirmed

    # ── Lógica AMD (espeja StreamDetector) ────────────────────────────────────

    def _classify(self) -> str | None:
        elapsed   = time.monotonic() - self._t0
        voice_dur = sum(self._voiced) * 0.032   # segundos con voz

        # Contar palabras: ráfagas de voz separadas por ≥320ms de silencio
        words   = 0
        in_word = False
        sil_run = 0
        wrd_run = 0
        for v in self._voiced:
            if v:
                sil_run  = 0
                wrd_run += 1
                in_word  = True
            else:
                if in_word:
                    sil_run += 1
                    if sil_run >= _SIL_WORD_END:
                        if wrd_run >= 3:   # ≥96ms de voz = cuenta como palabra (≈100ms de StreamDetector)
                            words += 1
                        in_word = False
                        wrd_run = 0

        log.info("Silero n=%d voice=%.2fs words=%d elapsed=%.1fs",
                 len(self._voiced), voice_dur, words, elapsed)

        if words >= 3:
            return "VOICEMAIL"

        if words >= 1 and not in_word and sil_run >= _SIL_HUMAN and elapsed >= _ELAPSED_HUMAN:
            return "HUMAN"

        if voice_dur > 2.0:
            return "VOICEMAIL"

        if elapsed > 1.5 and voice_dur < 0.1:
            return "VOICEMAIL"

        if elapsed > 4.5:
            return "HUMAN" if 0.1 <= voice_dur <= 1.5 else "VOICEMAIL"

        return None


# ── Singleton ─────────────────────────────────────────────────────────────────

_silero_vad: SileroVAD | None = None


def init_silero_vad(model_path: str) -> None:
    global _silero_vad
    if not model_path:
        return
    try:
        _silero_vad = SileroVAD(model_path)
    except Exception as e:
        log.warning("Silero VAD no disponible: %s", e)


def get_silero_vad() -> SileroVAD | None:
    return _silero_vad


def silero_loaded() -> bool:
    return _silero_vad is not None


def make_detector(engine: str):
    """
    Devuelve el detector correcto según engine ('silero' o 'stream').
    Si Silero no está cargado, cae a StreamDetector automáticamente.
    """
    if engine == "silero" and _silero_vad is not None:
        return SileroStreamDetector(_silero_vad)
    from app.core.amd_engine import StreamDetector
    return StreamDetector()
