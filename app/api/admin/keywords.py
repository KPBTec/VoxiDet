from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from app.config import settings
from app.api.admin.session import get_session, login_redirect
from app.api.admin._templates import templates
from app.db.keywords import get_all_keywords, add_keyword, delete_keyword, toggle_keyword
from app.core import keyword_cache

router = APIRouter()


@router.get("/keywords", response_class=HTMLResponse)
async def keywords_page(request: Request, msg: str = ""):
    if not get_session(request):
        return login_redirect(request)
    kws = await get_all_keywords()
    human     = [k for k in kws if k["type"] == "HUMAN"]
    voicemail = [k for k in kws if k["type"] == "VOICEMAIL"]
    return templates.TemplateResponse(request, "keywords.html", {
        "request":       request,
        "admin_prefix":  settings.ADMIN_PREFIX,
        "human":         human,
        "voicemail":     voicemail,
        "msg":           msg,
    })


@router.post("/keywords/add")
async def add_keyword_action(
    request: Request,
    word: str  = Form(...),
    type_: str = Form(..., alias="type"),
):
    if not get_session(request):
        return login_redirect(request)
    word = word.lower().strip()
    if not word or type_ not in ("HUMAN", "VOICEMAIL"):
        return RedirectResponse(
            url=f"{settings.ADMIN_PREFIX}/keywords?msg=error",
            status_code=302,
        )
    ok = await add_keyword(word, type_)
    if ok:
        await keyword_cache.refresh()
    return RedirectResponse(
        url=f"{settings.ADMIN_PREFIX}/keywords?msg={'ok' if ok else 'dup'}",
        status_code=302,
    )


@router.post("/keywords/{kw_id}/toggle")
async def toggle_keyword_action(request: Request, kw_id: int):
    if not get_session(request):
        return login_redirect(request)
    await toggle_keyword(kw_id)
    await keyword_cache.refresh()
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/keywords", status_code=302)


@router.post("/keywords/{kw_id}/delete")
async def delete_keyword_action(request: Request, kw_id: int):
    if not get_session(request):
        return login_redirect(request)
    await delete_keyword(kw_id)
    await keyword_cache.refresh()
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/keywords", status_code=302)
