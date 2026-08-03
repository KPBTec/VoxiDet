import logging
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from app.config import settings
from app.api.admin.session import create_session_cookie, get_session
from app.api.deps import get_real_ip
from app.db.admin_users import verify_admin_password

router = APIRouter()
log = logging.getLogger("voxidet.security")


def _templates():
    from app.api.admin._templates import templates
    return templates


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if get_session(request):
        return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients", status_code=302)
    return _templates().TemplateResponse(request, "login.html", {"request": request})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    admin = await verify_admin_password(username, password)
    if admin:
        response = RedirectResponse(url=f"{settings.ADMIN_PREFIX}/clients", status_code=302)
        create_session_cookie(response, admin["username"], admin["id"])
        return response
    log.warning("SECURITY_REJECT ip=%s reason=login_failed path=%s", get_real_ip(request), request.url.path)
    return _templates().TemplateResponse(
        request,
        "login.html",
        {"request": request, "error": "Usuario o contraseña incorrectos"},
        status_code=401,
    )


@router.get("/logout")
async def logout():
    response = RedirectResponse(url=f"{settings.ADMIN_PREFIX}/login", status_code=302)
    response.delete_cookie("amd_session")
    return response
