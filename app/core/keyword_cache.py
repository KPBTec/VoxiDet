"""
Caché en memoria de keywords AMD.
Se carga desde la BD al arrancar y se refresca cada 60s.
_classify_transcript() lee estas sets de forma síncrona.
"""

import asyncio
import logging

log = logging.getLogger("voxidet.keywords")

# asyncio.create_task() no retiene una referencia fuerte — sin guardarla en
# algún lado, el GC de CPython podría recolectar este loop de refresco a
# mitad de ejecución (mismo hallazgo de seguridad aplicado también en
# app/api/stream.py::_spawn_bg_task).
_bg_tasks: set[asyncio.Task] = set()

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


def _build_prompt() -> str:
    """initial_prompt para Whisper (Groq/OpenAI/Together/Fireworks) — lista de
    palabras esperadas separadas por coma, para sesgar el vocabulario sin
    restricción dura."""
    return ", ".join(sorted(_human | _voicemail)) + "."


# Precalculado (no recalculado en cada llamada a un proveedor) — antes stream.py
# hacía el join+sort de las keywords en CADA request HTTP saliente a 4 de los 7
# proveedores, cuando las keywords solo cambian cada 60s acá.
_prompt: str = _build_prompt()


def get_prompt() -> str:
    return _prompt


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
    global _human, _voicemail, _prompt
    try:
        from app.db.keywords import get_active_keywords
        h, v = await get_active_keywords()
        if h or v:
            _human, _voicemail = h, v
            log.debug("Keywords: %d HUMAN, %d VOICEMAIL", len(h), len(v))
    except Exception as e:
        log.warning("Error cargando keywords desde BD: %s", e)
    _prompt = _build_prompt()


async def start() -> None:
    await refresh()
    task = asyncio.create_task(_loop())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _loop() -> None:
    while True:
        await asyncio.sleep(60)
        await refresh()
