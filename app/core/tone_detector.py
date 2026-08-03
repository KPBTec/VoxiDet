"""
tone_detector.py — detección del tono de beep de buzón de voz vía Goertzel.

Twilio y Nexmo/Vonage usan el beep del buzón como señal fuerte: es un tono
puro (energía concentrada en una sola frecuencia), muy distinto de la voz
humana (energía repartida en banda ancha, varios formantes a la vez).
Goertzel calcula la energía en UNA frecuencia específica sin necesitar una
FFT completa — más barato, ideal para correr por chunk en tiempo real.

MODO ACTUAL: solo registra en logs (SECURITY... no, en log.debug), NO
participa todavía en la decisión HUMAN/VOICEMAIL. La frecuencia real del beep
depende del operador/central (AMD_BEEP_FREQ_HZ en credentials.conf, default 1000Hz) y no
hay datos de producción para calibrar el umbral con confianza — antes de
usarlo para decidir, revisar logs reales y confirmar qué tan seguido
"detected=True" coincide con VOICEMAIL real.

Validado con señales sintéticas antes de integrar: tono puro → ratio ~2.0,
ruido blanco / voz multi-formante → ratio <0.01 (separación de ~200x).
"""
import math

import numpy as np

_SAMPLE_RATE   = 8000
_HITS_REQUIRED = 3   # frames consecutivos con tono para confirmar (evita falsos positivos de 1 frame)
_RATIO_THRESHOLD = 0.5   # margen amplio sobre la separación observada (tono ~2.0 vs ruido/voz <0.01)


def _goertzel_energy(samples: np.ndarray, freq: float, sample_rate: int = _SAMPLE_RATE) -> float:
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(0.5 + (n * freq) / sample_rate)
    w = (2.0 * math.pi / n) * k
    coeff = 2.0 * math.cos(w)
    q1 = q2 = 0.0
    for s in samples:
        q0 = coeff * q1 - q2 + s
        q2 = q1
        q1 = q0
    return q1 * q1 + q2 * q2 - q1 * q2 * coeff


def _tone_ratio(frame: np.ndarray, freq: float) -> float:
    """Fracción de energía del frame concentrada en `freq` vs energía total.
    ~2.0 para un tono puro en esa frecuencia, <0.01 para ruido/voz."""
    samples = frame.astype(np.float64)
    n = len(samples)
    total_energy = float(np.sum(samples ** 2))
    if total_energy < 1e-6 or n == 0:
        return 0.0
    target_energy = _goertzel_energy(samples, freq)
    return (target_energy / (n * n / 4)) / (total_energy / n)


class ToneDetector:
    """Detecta un tono sostenido cerca de target_freq_hz frame a frame."""

    def __init__(self, target_freq_hz: float):
        self._freq = target_freq_hz
        self._consecutive_hits = 0
        self.confirmed = False   # una vez True, se queda así por el resto de la llamada

    def feed(self, frame: np.ndarray) -> bool:
        """frame: samples int16/float de un frame ya recortado a tamaño fijo.
        Retorna True si hay tono sostenido confirmado (≥_HITS_REQUIRED frames)."""
        ratio = _tone_ratio(frame, self._freq)
        if ratio > _RATIO_THRESHOLD:
            self._consecutive_hits += 1
        else:
            self._consecutive_hits = 0
        if self._consecutive_hits >= _HITS_REQUIRED:
            self.confirmed = True
        return self.confirmed
