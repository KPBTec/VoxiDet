"""
Caché en memoria de keywords AMD.
Se carga desde la BD al arrancar y se refresca cada 60s.
_classify_transcript() lee estas sets de forma síncrona.
"""

import asyncio
import logging

log = logging.getLogger("voxidet.keywords")

# Defaults — usados si la BD falla o aún no hay datos
_DEFAULT_HUMAN = {
    "aló", "alo", "hola", "bueno", "diga", "dígame",
    "sí", "yes", "hello", "habla",
    "buenos", "buenas", "espera",
}

_DEFAULT_VOICEMAIL = {
    "uno", "dos", "tres", "cuatro", "cinco",
    "seis", "siete", "ocho", "nueve", "cero",
    "buzón", "buzon", "mensaje", "disponible",
    "momento", "comuníquese", "comuniquese",
    "después", "despues", "marque", "deje",
    "grabación", "grabacion", "bip", "beep",
    "intentelo", "intento", "tono", "llamada",
    "comunicar", "atender", "operador",
    "gracias", "bienvenido", "bienvenida",
    "asistente", "llamadas", "servicio", "automatico", "automático",
    "dejas", "nombre", "motivo", "ausente", "ocupado",
}

_human:     set[str] = set(_DEFAULT_HUMAN)
_voicemail: set[str] = set(_DEFAULT_VOICEMAIL)


def get_human() -> set[str]:
    return _human


def get_voicemail() -> set[str]:
    return _voicemail


def get_human_words() -> set[str]:
    """Keywords HUMAN de una sola palabra (para set intersection rápido)."""
    return {k for k in _human if " " not in k}


def get_voicemail_words() -> set[str]:
    """Keywords VOICEMAIL de una sola palabra."""
    return {k for k in _voicemail if " " not in k}


def get_human_phrases() -> list[str]:
    """Keywords HUMAN multi-palabra (para substring matching)."""
    return [k for k in _human if " " in k]


def get_voicemail_phrases() -> list[str]:
    """Keywords VOICEMAIL multi-palabra (para substring matching)."""
    return [k for k in _voicemail if " " in k]


async def refresh() -> None:
    global _human, _voicemail
    try:
        from app.db.keywords import get_active_keywords
        h, v = await get_active_keywords()
        if h or v:
            _human, _voicemail = h, v
            log.debug("Keywords: %d HUMAN, %d VOICEMAIL", len(h), len(v))
    except Exception as e:
        log.warning("Error cargando keywords desde BD: %s", e)


async def start() -> None:
    await refresh()
    asyncio.create_task(_loop())


async def _loop() -> None:
    while True:
        await asyncio.sleep(60)
        await refresh()
