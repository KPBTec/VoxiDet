from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from app.config import settings

_SESSION_COOKIE = "amd_session"
_SESSION_MAX_AGE = 86400  # 24 horas


def _serializer() -> URLSafeTimedSerializer:
    if not settings.SECRET_KEY:
        raise RuntimeError("SECRET_KEY must be set in environment")
    return URLSafeTimedSerializer(settings.SECRET_KEY)


def create_session_cookie(response, user: str, user_id: int | None = None) -> None:
    token = _serializer().dumps({"user": user, "id": user_id})
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=_SESSION_MAX_AGE,
    )


def get_session(request: Request) -> dict | None:
    cookie = request.cookies.get(_SESSION_COOKIE)
    if not cookie:
        return None
    try:
        return _serializer().loads(cookie, max_age=_SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def login_redirect(request: Request):
    """Retorna RedirectResponse al login manteniendo el prefix correcto."""
    return RedirectResponse(
        url=f"{settings.ADMIN_PREFIX}/login",
        status_code=302,
    )


async def require_session(request: Request):
    """Dependencia FastAPI: redirige al login si no hay sesión válida."""
    if not get_session(request):
        raise HTTPException(
            status_code=302,
            headers={"Location": f"{settings.ADMIN_PREFIX}/login"},
        )
