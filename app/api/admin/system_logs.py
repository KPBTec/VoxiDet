"""
Sistema → Logs backend — ver los logs técnicos de la propia API (errores,
warnings, requests) desde el panel, sin entrar por SSH. Distinto de "Logs en
vivo" (/logs), que son detecciones de llamadas (HUMAN/VOICEMAIL/etc.), no
logs de aplicación.

Mismo criterio que backend/routers/system_logs.py de VoxiKam (solo lectura),
pero más simple: VoxiDet corre todo en un solo contenedor bajo gunicorn, sin
unidades systemd por componente, así que no hace falta journalctl — solo los
3 archivos rotados que ya escribe log_config.json. Allowlist fija (no glob,
no resolución de paths a partir de un nombre que mande el cliente) — más
simple todavía que el caso de VoxiKam porque el conjunto de archivos es
siempre el mismo, conocido de antemano.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.api.admin.session import require_session, get_session
from app.api.admin._templates import templates as _templates
from app.config import settings

router = APIRouter()

_SOURCES: dict[str, Path] = {
    "core":     Path("/srv/logs/core.log"),
    "web":      Path("/srv/logs/web.log"),
    "security": Path("/srv/logs/security.log"),
}


def _tail(source: str, lines: int) -> list[str]:
    path = _SOURCES.get(source)
    if not path or not path.exists():
        return []
    text = path.read_text(errors="replace")
    all_lines = [l for l in text.splitlines() if l.strip()]
    return all_lines[-lines:]


@router.get("/system/logs", response_class=HTMLResponse)
async def system_logs_page(request: Request, _=Depends(require_session)):
    return _templates.TemplateResponse(request, "system_logs.html", {
        "request":      request,
        "admin_prefix": settings.ADMIN_PREFIX,
        "active_page":  "system_logs",
        "sources":      list(_SOURCES.keys()),
    })


@router.get("/system/logs/data")
async def system_logs_data(request: Request, source: str = "core", lines: int = 200):
    if not get_session(request):
        return JSONResponse(status_code=403, content={"detail": "No autorizado"})
    if source not in _SOURCES:
        return JSONResponse(status_code=400, content={"detail": "source inválido"})
    lines = max(1, min(lines, 1000))
    return {"source": source, "lines": _tail(source, lines)}
