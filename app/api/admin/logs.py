import asyncio
import json
from datetime import date as date_type
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app.api.admin.session import get_session, login_redirect
from app.api.admin._templates import templates
from app.db.logs import get_recent_logs, get_logs_since
from app.config import settings

router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    if not get_session(request):
        return login_redirect(request)
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "admin_prefix": settings.ADMIN_PREFIX,
    })


@router.get("/logs/stream")
async def logs_stream(
    request: Request,
    caller_id: str | None = None,
    result:    str | None = None,
    uniqueid:  str | None = None,
    session:   str | None = None,
    date:      str | None = None,
):
    if not get_session(request):
        return login_redirect(request)

    # Un día pasado no va a recibir filas "nuevas" — se manda un lote grande
    # una sola vez y se cierra, en vez de dejar el polling corriendo cada 2s
    # para siempre sin sentido (y sin abrir una conexión SSE colgada de más).
    is_historical = bool(date) and date != date_type.today().isoformat()

    async def generator():
        initial = await get_recent_logs(
            client_id=0, limit=(500 if is_historical else 10),
            caller_filter=caller_id,
            result_filter=result,
            param1_filter=uniqueid,
            session_filter=session,
            date_filter=date,
        )
        # Enviamos en orden cronológico (más viejo primero)
        payload = json.dumps([_serialize(r) for r in reversed(initial)])
        yield f"event: init\ndata: {payload}\n\n"

        if is_historical:
            return

        # ID del último registro visto
        last_id = initial[0]["id"] if initial else 0

        # Polling cada 2s para nuevos registros
        while not await request.is_disconnected():
            await asyncio.sleep(2)
            rows = await get_logs_since(
                last_id=last_id,
                limit=50,
                caller_filter=caller_id,
                result_filter=result,
                param1_filter=uniqueid,
                session_filter=session,
                date_filter=date,
            )
            for row in rows:
                last_id = row["id"]
                yield f"data: {json.dumps(_serialize(row))}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


def _serialize(row: dict) -> dict:
    """Convierte tipos no serializables (datetime, Decimal) a string."""
    return {k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
            for k, v in row.items()}
