from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from app.config import settings
from app.api.admin.session import get_session, login_redirect
from app.api.admin._templates import templates
from app.db.sites import list_sites, create_site, rename_site, delete_site
from app.db.clients import get_all_clients_with_stats

router = APIRouter()


@router.get("/sites", response_class=HTMLResponse)
async def sites_page(request: Request):
    if not get_session(request):
        return login_redirect(request)
    sites   = await list_sites()
    clients = await get_all_clients_with_stats()
    counts: dict[int, int] = {}
    for c in clients:
        if c["site_id"]:
            counts[c["site_id"]] = counts.get(c["site_id"], 0) + 1
    return templates.TemplateResponse(request, "sites.html", {
        "request":      request,
        "sites":        sites,
        "counts":       counts,
        "admin_prefix": settings.ADMIN_PREFIX,
    })


@router.post("/sites/create")
async def create_site_action(request: Request, name: str = Form(...)):
    if not get_session(request):
        return login_redirect(request)
    if name.strip():
        await create_site(name)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/sites?created=1", status_code=302)


@router.post("/sites/{site_id}/rename")
async def rename_site_action(request: Request, site_id: int, name: str = Form(...)):
    if not get_session(request):
        return login_redirect(request)
    if name.strip():
        await rename_site(site_id, name)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/sites?saved=1", status_code=302)


@router.post("/sites/{site_id}/delete")
async def delete_site_action(request: Request, site_id: int):
    if not get_session(request):
        return login_redirect(request)
    await delete_site(site_id)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/sites?deleted=1", status_code=302)
