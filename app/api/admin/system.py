import psutil
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.api.admin.session import require_session, get_session
from app.api.admin._templates import templates as _templates
from app.config import settings
from app.db.settings import get_setting, set_setting

router = APIRouter()


def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _get_stats() -> dict:
    cpu   = psutil.cpu_percent(interval=None)
    mem   = psutil.virtual_memory()
    disk  = psutil.disk_usage("/")

    net_raw = psutil.net_io_counters(pernic=True)
    net = [
        {
            "iface":  iface,
            "rx_str": _fmt_bytes(c.bytes_recv),
            "tx_str": _fmt_bytes(c.bytes_sent),
            "rx_bytes": c.bytes_recv,
            "tx_bytes": c.bytes_sent,
        }
        for iface, c in net_raw.items()
        if iface != "lo"
    ]

    return {
        "cpu_percent":   cpu,
        "ram_percent":   mem.percent,
        "ram_used_gb":   round(mem.used  / 1024 ** 3, 2),
        "ram_total_gb":  round(mem.total / 1024 ** 3, 1),
        "disk_percent":  disk.percent,
        "disk_used_gb":  round(disk.used  / 1024 ** 3, 1),
        "disk_total_gb": round(disk.total / 1024 ** 3, 1),
        "net":           net,
    }


@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request, _=Depends(require_session)):
    from app.api.amd import active_calls
    stats = _get_stats()
    public_url = (await get_setting("public_url") or settings.PUBLIC_URL or "").strip()
    # Mostrar siempre con esquema — si viene de credentials.conf sin "http://"
    # (ej. "10.100.10.15:8000"), mismo auto-fix que ya se aplica al guardar,
    # para que el campo nunca muestre una URL ambigua sobre si necesita http(s)://.
    if public_url and "://" not in public_url:
        public_url = "http://" + public_url
    return _templates.TemplateResponse(request, "system.html", {
        "request":      request,
        "admin_prefix": settings.ADMIN_PREFIX,
        "active_page":  "system",
        "stats":        stats,
        "active_calls": active_calls,
        "public_url":   public_url,
    })


@router.post("/system/public-url")
async def update_public_url(
    request: Request,
    public_url: str = Form(...),
    _=Depends(require_session),
):
    """Dominio/IP que se hornea en el AGI descargado (/install/<token>,
    /amd/update) — vive en MySQL, no en credentials.conf (ver app/db/settings.py:
    el contenedor recibe ese archivo vía env_file:, nunca montado como
    filesystem, la app no puede reescribirlo). Solo afecta AGIs que se
    descarguen o actualicen de acá en más — los nodos ya instalados con la
    URL vieja siguen así hasta su próximo auto-update."""
    public_url = public_url.strip().rstrip("/")
    if public_url and "://" not in public_url:
        public_url = "http://" + public_url
    await set_setting("public_url", public_url)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/system", status_code=302)


@router.get("/system/data")
async def system_data(request: Request):
    if not get_session(request):
        return JSONResponse(status_code=403, content={"detail": "No autorizado"})
    from app.api.amd import active_calls
    return {**_get_stats(), "active_calls": active_calls}
