import secrets
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from app.config import settings
from app.api.admin.session import get_session, login_redirect
from app.api.admin._templates import templates
from app.db.clients import (
    get_all_clients_with_stats,
    create_client,
    toggle_client_active,
    update_client_ips,
    update_client_limit,
    update_client_name,
    update_client_provider,
    rotate_api_key,
    rotate_install_token,
    delete_client,
    set_amd_mode,
    set_amd_bias,
)
from app.cache.client_cache import invalidate_api_key
from app.db.audit import log_audit

router = APIRouter()


def _gen() -> str:
    return secrets.token_urlsafe(36)


async def _get_client(client_id: int) -> dict | None:
    """Registro actual de un cliente puntual — reusa get_all_clients_with_stats()
    (ya usado igual en client_keywords_page más abajo). No es la ruta caliente
    de detección (es acción de panel admin, baja frecuencia), así que traer
    la lista completa para filtrar un id es aceptable acá."""
    clients = await get_all_clients_with_stats()
    return next((c for c in clients if c["id"] == client_id), None)


def _admin_user(request: Request) -> str:
    sess = get_session(request)
    return sess.get("user", "?") if sess else "?"


@router.get("/clients", response_class=HTMLResponse)
async def clients_page(request: Request, site_id: str = ""):
    if not get_session(request):
        return login_redirect(request)
    from app.db.providers import get_active_providers
    from app.db.sites import list_sites
    _site_id_int      = int(site_id) if site_id else None
    clients           = await get_all_clients_with_stats(_site_id_int)
    active_providers  = await get_active_providers()
    sites             = await list_sites()
    return templates.TemplateResponse(request, "clients.html", {
        "request":          request,
        "clients":          clients,
        "active_providers": active_providers,
        "sites":            sites,
        "selected_site":    _site_id_int,
        "public_url":       settings.PUBLIC_URL.rstrip("/"),
        "admin_prefix":     settings.ADMIN_PREFIX,
    })


@router.post("/clients/create")
async def create_client_action(
    request: Request,
    name: str = Form(...),
    limit: int = Form(500000),
    provider: str = Form("groq"),
    ips: str = Form(""),
    notes: str = Form(""),
    site_id: str = Form(""),
):
    if not get_session(request):
        return login_redirect(request)

    api_key = _gen()
    install_token = _gen()
    new_id = await create_client(name, limit, api_key, install_token, provider, ips, notes)
    if site_id:
        from app.db.sites import set_client_site
        await set_client_site(new_id, int(site_id))
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients?created=1", status_code=302)


@router.post("/clients/{client_id}/limit")
async def update_limit(
    request: Request,
    client_id: int,
    limit: int = Form(...),
):
    if not get_session(request):
        return login_redirect(request)
    old = await _get_client(client_id)
    await update_client_limit(client_id, limit)
    if old:
        await log_audit(_admin_user(request), client_id, "daily_limit", old["daily_limit"], limit)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients?saved=1", status_code=302)


@router.post("/clients/{client_id}/name")
async def update_name(
    request: Request,
    client_id: int,
    name: str = Form(...),
):
    if not get_session(request):
        return login_redirect(request)
    old = await _get_client(client_id)
    await update_client_name(client_id, name)
    if old:
        await log_audit(_admin_user(request), client_id, "name", old["name"], name)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients?saved=1", status_code=302)


@router.post("/clients/{client_id}/provider")
async def update_provider(
    request: Request,
    client_id: int,
    provider: str = Form(...),
):
    if not get_session(request):
        return login_redirect(request)
    old = await _get_client(client_id)
    await update_client_provider(client_id, provider)
    if old:
        await log_audit(_admin_user(request), client_id, "provider", old["provider"], provider)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients", status_code=302)


@router.post("/clients/{client_id}/site")
async def update_site(request: Request, client_id: int, site_id: str = Form("")):
    if not get_session(request):
        return login_redirect(request)
    from app.db.sites import set_client_site
    old = await _get_client(client_id)
    new_site_id = int(site_id) if site_id else None
    await set_client_site(client_id, new_site_id)
    if old:
        await log_audit(_admin_user(request), client_id, "site_id", old["site_id"], new_site_id)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients", status_code=302)


@router.post("/clients/{client_id}/toggle")
async def toggle_active(request: Request, client_id: int):
    if not get_session(request):
        return login_redirect(request)
    new_state = await toggle_client_active(client_id)
    await log_audit(_admin_user(request), client_id, "active", not new_state, new_state)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients", status_code=302)


@router.post("/clients/{client_id}/amd-mode")
async def set_amd_mode_action(request: Request, client_id: int, mode: str = Form(...)):
    if not get_session(request):
        return login_redirect(request)
    await set_amd_mode(client_id, mode)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients", status_code=302)


@router.post("/clients/{client_id}/amd-bias")
async def set_amd_bias_action(request: Request, client_id: int, bias: str = Form(...)):
    if not get_session(request):
        return login_redirect(request)
    await set_amd_bias(client_id, bias)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients", status_code=302)


@router.post("/clients/{client_id}/keywords-mode")
async def set_keywords_mode_action(request: Request, client_id: int, mode: str = Form(...)):
    if not get_session(request):
        return login_redirect(request)
    from app.db.clients import set_keywords_mode
    from app.db.client_keywords import invalidate_client_keywords_cache
    await set_keywords_mode(client_id, mode)
    await invalidate_client_keywords_cache(client_id)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients", status_code=302)


@router.post("/clients/{client_id}/ips")
async def update_ips(
    request: Request,
    client_id: int,
    ips: str = Form(""),
):
    if not get_session(request):
        return login_redirect(request)
    old = await _get_client(client_id)
    await update_client_ips(client_id, ips.strip())
    if old:
        await log_audit(_admin_user(request), client_id, "allowed_ips", old["allowed_ips"], ips.strip())
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients?saved=1", status_code=302)


@router.post("/clients/{client_id}/rotate-key")
async def rotate_key(request: Request, client_id: int):
    if not get_session(request):
        return login_redirect(request)
    old_key = await rotate_api_key(client_id, _gen())
    if old_key:
        await invalidate_api_key(old_key)
    # Nunca se guarda el valor real de la key en el audit log (sería un
    # secreto legible por cualquier otro admin) — solo se registra que la
    # acción ocurrió.
    await log_audit(_admin_user(request), client_id, "api_key", "***", "rotada")
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients?rotated=1", status_code=302)


@router.post("/clients/{client_id}/rotate-token")
async def rotate_token(request: Request, client_id: int):
    if not get_session(request):
        return login_redirect(request)
    await rotate_install_token(client_id, _gen())
    await log_audit(_admin_user(request), client_id, "install_token", "***", "rotado")
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients?rotated=1", status_code=302)


@router.post("/clients/{client_id}/delete")
async def delete_client_action(request: Request, client_id: int):
    if not get_session(request):
        return login_redirect(request)
    old = await _get_client(client_id)
    await delete_client(client_id)
    if old:
        await log_audit(_admin_user(request), client_id, "cliente", old["name"], "eliminado")
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients?deleted=1", status_code=302)


@router.get("/clients/{client_id}/audit", response_class=HTMLResponse)
async def client_audit_page(request: Request, client_id: int):
    if not get_session(request):
        return login_redirect(request)
    client = await _get_client(client_id)
    if not client:
        return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients", status_code=302)
    from app.db.audit import get_client_audit_log
    entries = await get_client_audit_log(client_id)
    return templates.TemplateResponse(request, "client_audit.html", {
        "request":      request,
        "client":       client,
        "entries":      entries,
        "admin_prefix": settings.ADMIN_PREFIX,
    })


# ── Keywords por cliente ───────────────────────────────────────────────────────

@router.get("/clients/{client_id}/keywords", response_class=HTMLResponse)
async def client_keywords_page(request: Request, client_id: int):
    if not get_session(request):
        return login_redirect(request)
    from app.db.client_keywords import get_all_client_keywords
    from app.db.clients import get_all_clients_with_stats
    clients = await get_all_clients_with_stats()
    client  = next((c for c in clients if c["id"] == client_id), None)
    if not client:
        return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients", status_code=302)
    kws     = await get_all_client_keywords(client_id)
    human     = [k for k in kws if k["type"] == "HUMAN"]
    voicemail = [k for k in kws if k["type"] == "VOICEMAIL"]
    return templates.TemplateResponse(request, "client_keywords.html", {
        "request":      request,
        "client":       client,
        "human":        human,
        "voicemail":    voicemail,
        "admin_prefix": settings.ADMIN_PREFIX,
    })


@router.post("/clients/{client_id}/keywords/add")
async def add_client_keyword_action(
    request: Request,
    client_id: int,
    word: str   = Form(...),
    type_: str  = Form(..., alias="type"),
):
    if not get_session(request):
        return login_redirect(request)
    from app.db.client_keywords import add_client_keyword
    word = word.lower().strip()
    if word and type_ in ("HUMAN", "VOICEMAIL"):
        await add_client_keyword(client_id, word, type_)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients/{client_id}/keywords", status_code=302)


@router.post("/clients/{client_id}/keywords/{kw_id}/toggle")
async def toggle_client_keyword_action(request: Request, client_id: int, kw_id: int):
    if not get_session(request):
        return login_redirect(request)
    from app.db.client_keywords import toggle_client_keyword
    await toggle_client_keyword(kw_id, client_id)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients/{client_id}/keywords", status_code=302)


@router.post("/clients/{client_id}/keywords/{kw_id}/delete")
async def delete_client_keyword_action(request: Request, client_id: int, kw_id: int):
    if not get_session(request):
        return login_redirect(request)
    from app.db.client_keywords import delete_client_keyword
    await delete_client_keyword(kw_id, client_id)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients/{client_id}/keywords", status_code=302)
