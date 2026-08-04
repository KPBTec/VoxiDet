from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from app.api.deps import require_admin
from app.api.admin import (
    auth, clients, logs, keywords, providers, firewall, system, system_logs,
    timeseries, reports, users, dashboard, sites,
)
from app.config import settings

router = APIRouter()

# Rutas del CMS (sesión web)
router.include_router(auth.router)
router.include_router(dashboard.router)
router.include_router(clients.router)
router.include_router(sites.router)
router.include_router(logs.router)
router.include_router(keywords.router)
router.include_router(providers.router)
router.include_router(firewall.router)
router.include_router(system.router)
router.include_router(system_logs.router)
router.include_router(timeseries.router)
router.include_router(reports.router)
router.include_router(users.router)

# Redirect raíz del admin → Dashboard
@router.get("/")
async def admin_root():
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/dashboard", status_code=302)

# API JSON para acceso programático (X-Admin-Key)
_api = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])

from app.db.logs import get_recent_logs
from app.db.usage import get_all_daily_stats


@_api.get("/logs")
async def api_logs(
    limit    : int        = 50,
    caller_id: str | None = None,
    result   : str | None = None,
    uniqueid : str | None = None,
):
    limit = min(limit, 500)
    if result and result not in ("HUMAN", "VOICEMAIL", "UNKNOWN"):
        raise HTTPException(status_code=400, detail="result: HUMAN | VOICEMAIL | UNKNOWN")
    rows = await get_recent_logs(
        client_id=0, limit=limit,
        caller_filter=caller_id, result_filter=result, param1_filter=uniqueid,
    )
    return {"count": len(rows), "logs": rows}


@_api.get("/stats")
async def api_stats():
    return {"clients": await get_all_daily_stats()}


router.include_router(_api)
