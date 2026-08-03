import json
import subprocess
import sys
import logging
from pathlib import Path
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from app.config import settings
from app.api.admin.session import get_session, login_redirect
from app.api.admin._templates import templates
from app.db.firewall import list_rules, add_rule, toggle_rule, delete_rule, queue_unban

log = logging.getLogger("voxidet.firewall")
router = APIRouter()

_SCRIPTS = Path(__file__).parent.parent.parent.parent / "scripts"

_VALID_ACTIONS  = {"allow", "deny"}
_VALID_SERVICES = {"all", "api", "ssh"}

# Escrito por scripts/fail2ban_bridge.py (host, vía cron) en el bind mount de
# logs — el contenedor no puede llamar fail2ban-client directo.
_FAIL2BAN_STATUS = Path("/srv/logs/fail2ban-status.json")
_FAIL2BAN_JAILS  = ["sshd", "voxidet-security"]


def _read_fail2ban_status() -> dict:
    try:
        return json.loads(_FAIL2BAN_STATUS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {jail: [] for jail in _FAIL2BAN_JAILS}


def _sync():
    """Regenera y aplica nftables en background."""
    script = _SCRIPTS / "gen_nftables.py"
    if script.exists():
        subprocess.Popen([sys.executable, str(script)])
    else:
        log.warning("gen_nftables.py no encontrado en %s", _SCRIPTS)


@router.get("/firewall", response_class=HTMLResponse)
async def firewall_page(request: Request):
    if not get_session(request):
        return login_redirect(request)
    rules = await list_rules()
    return templates.TemplateResponse(request, "firewall.html", {
        "request":         request,
        "rules":           rules,
        "fail2ban_status": _read_fail2ban_status(),
        "admin_prefix":    settings.ADMIN_PREFIX,
        "active_page":     "firewall",
    })


@router.post("/firewall/fail2ban/unban")
async def firewall_fail2ban_unban(
    request: Request,
    jail:    str = Form(...),
    ip:      str = Form(...),
):
    """Encola el unban — un script de host (cron cada minuto) lo ejecuta de
    verdad, el contenedor no puede llamar fail2ban-client directo."""
    if not get_session(request):
        return login_redirect(request)
    if jail in _FAIL2BAN_JAILS:
        await queue_unban(jail, ip.strip())
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/firewall?unban_queued=1", status_code=302)


@router.post("/firewall/add")
async def firewall_add(
    request:     Request,
    ip:          str = Form(...),
    action:      str = Form("deny"),
    service:     str = Form("all"),
    description: str = Form(""),
):
    if not get_session(request):
        return login_redirect(request)

    ip      = ip.strip()
    action  = action  if action  in _VALID_ACTIONS  else "deny"
    service = service if service in _VALID_SERVICES else "all"

    if not ip:
        return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/firewall?err=ip", status_code=302)

    await add_rule(ip, action, service, description.strip())
    _sync()
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/firewall?added=1", status_code=302)


@router.post("/firewall/{rule_id}/toggle")
async def firewall_toggle(request: Request, rule_id: int):
    if not get_session(request):
        return login_redirect(request)
    await toggle_rule(rule_id)
    _sync()
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/firewall", status_code=302)


@router.post("/firewall/{rule_id}/delete")
async def firewall_delete(request: Request, rule_id: int):
    if not get_session(request):
        return login_redirect(request)
    await delete_rule(rule_id)
    _sync()
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/firewall?deleted=1", status_code=302)


@router.post("/firewall/sync")
async def firewall_sync(request: Request):
    """Fuerza re-aplicar nftables desde el panel."""
    if not get_session(request):
        return login_redirect(request)
    _sync()
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/firewall?synced=1", status_code=302)
