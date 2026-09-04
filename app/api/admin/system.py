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

    # Modo ARI (experimental, v1.28.0) — mismo mecanismo que public_url de
    # arriba: override en MySQL (app_settings, editable acá) por encima de
    # credentials.conf, porque el panel no puede reescribir ese archivo (ver
    # docstring de app/db/settings.py). La password NUNCA se manda de vuelta
    # al formulario (ver update_ari_settings) — el campo queda vacío y solo
    # se pisa si se tipea una nueva.
    ari_settings = {
        "ari_url":          (await get_setting("ari_url")   or settings.ARI_URL),
        "ari_user":         (await get_setting("ari_user")  or settings.ARI_USER),
        "ari_app":          (await get_setting("ari_app")   or settings.ARI_APP),
        "ari_media_mode":   (await get_setting("ari_media_mode") or settings.ARI_MEDIA_MODE),
        "audiosocket_host": (await get_setting("audiosocket_host") or settings.AUDIOSOCKET_HOST),
        "ari_password_set": bool(await get_setting("ari_password") or settings.ARI_PASSWORD),
    }

    return _templates.TemplateResponse(request, "system.html", {
        "request":      request,
        "admin_prefix": settings.ADMIN_PREFIX,
        "active_page":  "system",
        "stats":        stats,
        "active_calls": active_calls,
        "public_url":   public_url,
        "ari":          ari_settings,
    })


@router.post("/system/ari-settings")
async def update_ari_settings(
    request: Request,
    ari_url:          str = Form(""),
    ari_user:         str = Form(""),
    ari_password:     str = Form(""),
    ari_app:          str = Form(""),
    ari_media_mode:   str = Form("rtp"),
    audiosocket_host: str = Form(""),
    _=Depends(require_session),
):
    """Modo ARI (EXPERIMENTAL, v1.28.0) — ver app/ari/controller.py. Guarda
    en MySQL (app_settings), no en credentials.conf (mismo motivo que
    public_url: el panel no puede reescribir ese archivo). El contenedor
    `ari-controller` lee este override al arrancar (ver
    app/ari/controller.py::run()) — un cambio acá requiere reiniciarlo
    (`docker compose restart ari-controller`) para tomar efecto, no es
    hot-reload — es una conexión WebSocket persistente de larga vida, no
    tiene sentido reconectarla sola en caliente por cada cambio de config."""
    await set_setting("ari_url", ari_url.strip().rstrip("/"))
    await set_setting("ari_user", ari_user.strip())
    if ari_password.strip():
        # Campo vacío = "no cambiar" — nunca se manda la password actual de
        # vuelta al formulario, así que un submit sin tocar ese campo no
        # debe borrar la que ya estaba guardada.
        await set_setting("ari_password", ari_password.strip())
    await set_setting("ari_app", ari_app.strip() or "voxidet-ari")
    await set_setting("ari_media_mode", ari_media_mode.strip() or "rtp")
    await set_setting("audiosocket_host", audiosocket_host.strip())
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/system?ari_saved=1", status_code=302)


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
