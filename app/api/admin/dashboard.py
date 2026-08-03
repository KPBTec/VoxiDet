from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.api.admin.session import require_session, get_session
from app.api.admin._templates import templates as _templates
from app.api.admin.system import _get_stats
from app.config import settings
from app.db.usage import get_today_summary
from app.db.clients import count_active_clients

router = APIRouter()


async def _dashboard_data() -> dict:
    from app.api.amd import active_calls
    stats   = _get_stats()
    summary = await get_today_summary()
    active_clients = await count_active_clients()
    return {**stats, "active_calls": active_calls, "summary": summary, "active_clients": active_clients}


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request, _=Depends(require_session)):
    data = await _dashboard_data()
    return _templates.TemplateResponse(request, "dashboard.html", {
        "request":      request,
        "admin_prefix": settings.ADMIN_PREFIX,
        "active_page":  "dashboard",
        **data,
    })


@router.get("/dashboard/data")
async def dashboard_data(request: Request):
    if not get_session(request):
        return JSONResponse(status_code=403, content={"detail": "No autorizado"})
    return await _dashboard_data()
