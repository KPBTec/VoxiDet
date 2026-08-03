from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from app.config import settings
from app.api.admin.session import get_session, login_redirect
from app.api.admin._templates import templates
from app.db.admin_users import (
    list_admin_users,
    create_admin_user,
    set_admin_active,
    update_admin_password,
    get_admin_by_username,
)

router = APIRouter()


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, msg: str = ""):
    sess = get_session(request)
    if not sess:
        return login_redirect(request)
    users = await list_admin_users()
    return templates.TemplateResponse(request, "users.html", {
        "request":      request,
        "users":        users,
        "current_user": sess.get("user"),
        "admin_prefix": settings.ADMIN_PREFIX,
        "msg":          msg,
    })


@router.post("/users/create")
async def create_user_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if not get_session(request):
        return login_redirect(request)
    username = username.strip()
    if len(username) < 3 or len(password) < 8:
        return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/users?msg=error", status_code=302)
    if await get_admin_by_username(username):
        return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/users?msg=dup", status_code=302)
    await create_admin_user(username, password)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/users?msg=ok", status_code=302)


@router.post("/users/{user_id}/toggle")
async def toggle_user_action(request: Request, user_id: int, active: str = Form(...)):
    sess = get_session(request)
    if not sess:
        return login_redirect(request)
    ok = await set_admin_active(user_id, active == "1")
    return RedirectResponse(
        url=f"{settings.ADMIN_PREFIX}/users?msg={'ok' if ok else 'last-admin'}", status_code=302
    )


@router.post("/users/{user_id}/password")
async def update_password_action(request: Request, user_id: int, password: str = Form(...)):
    if not get_session(request):
        return login_redirect(request)
    if len(password) < 8:
        return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/users?msg=error", status_code=302)
    await update_admin_password(user_id, password)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/users?msg=pw-ok", status_code=302)
