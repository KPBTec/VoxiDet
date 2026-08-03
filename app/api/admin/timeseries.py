from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.api.admin.session import require_session, get_session
from app.api.admin._templates import templates as _templates
from app.config import settings
from app.db.timeseries import query_timeseries_live, query_timeseries_day
from app.db.clients import get_all_clients_with_stats

router = APIRouter()


@router.get("/timeseries", response_class=HTMLResponse)
async def timeseries_page(request: Request, _=Depends(require_session)):
    clients = await get_all_clients_with_stats()
    return _templates.TemplateResponse(request, "timeseries.html", {
        "request":      request,
        "admin_prefix": settings.ADMIN_PREFIX,
        "active_page":  "timeseries",
        "clients":      clients,
        "today_str":    date_type.today().isoformat(),
    })


@router.get("/timeseries/data")
async def timeseries_data(
    request:   Request,
    range:     int           = Query(1, ge=1, le=12),
    date:      Optional[str] = Query(None),
    client_id: Optional[str] = Query(None),   # "" cuando el selector es "Todos los clientes" — no un int siempre
):
    from fastapi.responses import JSONResponse
    if not get_session(request):
        return JSONResponse(status_code=403, content={"detail": "No autorizado"})
    client_id = int(client_id) if client_id else None
    if date:
        day = date_type.fromisoformat(date)
        return await query_timeseries_day(day, client_id)
    return await query_timeseries_live(range, client_id)
