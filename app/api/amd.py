import asyncio
import hashlib
import logging

from fastapi import APIRouter, Header, UploadFile, File, HTTPException, Depends, Request
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.api.deps import verify_client, verify_client_readonly
from app.core.amd_engine import detect, transcribe_for_log
from app.core.agi_template import TEMPLATE
from app.db.logs import save_log

_AGI_VERSION = hashlib.sha256(TEMPLATE.encode()).hexdigest()[:12]

log = logging.getLogger("voxidet.api")
router = APIRouter()

_MAX_BYTES = int(settings.AUDIO_MAX_SECONDS * 8000 * 2 + 512)


def _log_task_exception(task: "asyncio.Task") -> None:
    """Segunda red de seguridad para tasks de background (save_log, etc.) —
    save_log() ya atrapa sus propios errores, pero esto cubre cualquier otra
    excepción de la task completa (ej. en transcribe_for_log(), antes de
    llegar a save_log) que si no quedaría solo como un "Task exception was
    never retrieved" de asyncio que nadie mira."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error("Tarea de background falló: %s", exc, exc_info=exc)

# Llamadas en proceso ahora mismo (incrementa al entrar, decrementa al salir)
active_calls: int = 0


@router.post("/amd")
async def amd_detect(
    audio      : UploadFile = File(...),
    call_id    : str = Header(default="", alias="X-Call-ID"),
    caller_id  : str = Header(default="", alias="X-Caller-ID"),
    lead_id    : str = Header(default="", alias="X-Lead-ID"),
    campaign_id: str = Header(default="", alias="X-Campaign-ID"),
    list_id    : str = Header(default="", alias="X-List-ID"),
    param1     : str = Header(default="", alias="X-Param-1"),
    param2     : str = Header(default="", alias="X-Param-2"),
    param3     : str = Header(default="", alias="X-Param-3"),
    param4     : str = Header(default="", alias="X-Param-4"),
    client     : dict = Depends(verify_client),
):
    """
    Detección AMD principal.

    Headers requeridos : X-API-Key
    Headers opcionales : X-Call-ID, X-Caller-ID, X-Lead-ID, X-Campaign-ID, X-List-ID
                         X-Param-1..4 (custom, uso genérico — X-Lead/Campaign/List-ID
                         tienen prioridad y se mapean igual que en modo stream)
    Body               : WAV multipart, campo 'audio', máx 3s, 8kHz 16bit mono
    """
    audio_bytes = await audio.read(_MAX_BYTES + 1)
    if len(audio_bytes) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="Audio demasiado largo")
    if len(audio_bytes) < 44:   # 44 = tamaño mínimo de header WAV
        raise HTTPException(status_code=400, detail="Audio vacío o inválido")

    global active_calls
    active_calls += 1
    try:
        # active_providers no se resuelve acá — detect() lo hace por su cuenta
        # solo si capa 1 no alcanza a decidir, para no atar el camino rápido
        # (capa 1, sin red/DB) a una consulta que la mayoría de las llamadas
        # no necesita.
        detection = await detect(
            audio_bytes,
            provider=client.get("provider", "groq"),
            aggressive=client.get("amd_bias") == "aggressive",
        )
    finally:
        active_calls -= 1

    log.info(
        "[%s] %s %s → %s  layer=%s  provider=%s  %dms",
        (call_id or "sin-id")[:20], client["name"], caller_id or "unknown",
        detection["result"], detection["layer_used"], detection.get("provider", ""),
        detection["latency_ms"],
    )

    # X-Lead-ID/Campaign-ID/List-ID (enviados por el AGI actual) tienen
    # prioridad sobre X-Param-1..4 genéricos — mismo mapeo que usa el modo
    # stream (param1=lead_id, param4=campaign_id|list_id) para que el panel
    # muestre lo mismo sin importar qué modo usó cada llamada. El transcript
    # ahora tiene columna dedicada (antes vivía en param3, VARCHAR(100) —
    # se truncaba en silencio, ver migración ensure_mode_transcript_columns).
    _p1 = lead_id or param1
    _p4 = (f"{campaign_id}|{list_id}" if campaign_id else list_id) or param4

    if detection["transcript"]:
        _transcript = detection["transcript"][:200]
    else:
        _transcript = "[sin audio]" if len(audio_bytes) <= 44 else "[silencio]"

    _log_kwargs = dict(
        client_id  = client["id"],
        call_id    = call_id[:100]  if call_id   else "",
        caller_id  = caller_id[:50] if caller_id else "",
        result     = detection["result"],
        layer      = detection["layer_used"],
        mode       = "batch",
        provider   = detection.get("provider", ""),
        latency_ms = detection["latency_ms"],
        audio_secs = round(len(audio_bytes) / (8000 * 2), 2),
        param1     = _p1[:100] if _p1 else "",
        param2     = param2[:100] if param2 else "",
        param3     = param3[:100] if param3 else "",
        param4     = _p4[:100] if _p4 else "",
    )

    if detection["layer_used"] == 1:
        # La capa 1 ya decidió AMDSTATUS (rápido) — igual transcribimos en
        # segundo plano solo para que quede en el log como registro/
        # auditoría, sin retrasar la respuesta que ya se le va a dar a
        # Asterisk. La decisión de AMD ya está tomada, esto no la cambia.
        t = asyncio.create_task(_log_with_background_transcript(
            audio_bytes, client.get("provider", "groq"), _log_kwargs,
        ))
        t.add_done_callback(_log_task_exception)
    else:
        t = asyncio.create_task(save_log(transcript=_transcript, **_log_kwargs))
        t.add_done_callback(_log_task_exception)

    return {
        "status":     detection["result"],
        "latency_ms": detection["latency_ms"],
        "layer":      detection["layer_used"],
    }


async def _log_with_background_transcript(audio_bytes, provider, log_kwargs):
    # active_providers no se pasa — transcribe_for_log() lo resuelve por su
    # cuenta (corre en una task de background, no bloquea la respuesta a
    # Asterisk, así que el costo de la consulta no afecta la latencia real).
    used_provider, transcript = await transcribe_for_log(audio_bytes, provider)
    if used_provider:
        log_kwargs["provider"] = used_provider
    transcript = transcript[:200] if transcript else "[silencio]"
    await save_log(transcript=transcript, **log_kwargs)


@router.get("/amd/check")
async def amd_check(client: dict = Depends(verify_client_readonly)):
    """
    El AGI llama este endpoint al inicio de cada llamada.
    Devuelve el modo activo (batch/stream) y la versión del AGI en el servidor.
    Si la versión difiere, el AGI se auto-actualiza vía /amd/update.
    No consume límite diario.
    """
    return {
        "mode":    client.get("amd_mode", "batch"),
        "version": _AGI_VERSION,
    }


@router.get("/amd/update")
async def amd_update(
    request : Request,
    x_api_key: str = Header(..., alias="X-API-Key"),
    client  : dict = Depends(verify_client_readonly),
):
    """
    Devuelve el AGI actualizado autenticando por API key (sin install_token).
    Usado por el auto-update del AGI cuando detecta versión desactualizada.
    No consume límite diario.
    """
    from app.api.install import _resolve_server_url
    server_url = await _resolve_server_url(request)
    script = (TEMPLATE
        .replace("__SERVER__",  server_url)
        # client["api_key"] no existe — get_client_by_apikey() no selecciona
        # esa columna (solo get_client_by_install_token() lo hace, para
        # /install/<token>). Usamos directo el header ya validado por
        # verify_client_readonly — es la misma key, sin necesidad de traerla
        # de nuevo de la DB. Sin este fix, /amd/update tiraba 500
        # (KeyError: 'api_key') cada vez que el AGI detectaba una versión
        # nueva y trataba de auto-actualizarse.
        .replace("__APIKEY__",  x_api_key)
        .replace("__CLIENT__",  client["name"])
        .replace("__VERSION__", _AGI_VERSION)
    )
    return PlainTextResponse(
        content=script,
        headers={"Content-Disposition": "attachment; filename=amd_ia.agi"},
    )
