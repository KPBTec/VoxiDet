import logging
from fastapi import Request, Header, HTTPException

from app.config import settings
from app.core.ip_utils import ip_allowed
from app.cache.client_cache import get_client_cached, check_and_increment_limit

log = logging.getLogger("voxidet.deps")


def get_real_ip(request: Request) -> str:
    """Obtiene IP real respetando Cloudflare (CF-Connecting-IP) y proxies."""
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip().split(",")[0].strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def verify_client(
    request: Request,
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> dict:
    """
    Dependencia compartida: valida API key → Redis → MySQL.
    Verifica activo, IP whitelist y límite diario.
    """
    real_ip = get_real_ip(request)

    if not x_api_key or len(x_api_key) < 32:
        log.warning("SECURITY_REJECT ip=%s reason=invalid_api_key path=%s", real_ip, request.url.path)
        raise HTTPException(status_code=401, detail="API key inválida")

    client = await get_client_cached(x_api_key)

    if not client:
        log.warning("SECURITY_REJECT ip=%s reason=invalid_api_key path=%s", real_ip, request.url.path)
        raise HTTPException(status_code=401, detail="API key no autorizada")

    if not client["active"]:
        log.warning("Cliente inactivo: id=%s", client["id"])
        raise HTTPException(status_code=403, detail="Cliente inactivo")

    if not ip_allowed(real_ip, client.get("allowed_ips")):
        log.warning("SECURITY_REJECT ip=%s reason=ip_not_allowed path=%s", real_ip, request.url.path)
        raise HTTPException(status_code=403, detail="IP no autorizada")

    if not await check_and_increment_limit(client["id"], client["daily_limit"]):
        raise HTTPException(status_code=429, detail="Límite diario excedido")

    return client


async def verify_client_readonly(
    request: Request,
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> dict:
    """Como verify_client pero sin incrementar el límite diario. Para /amd/check y /amd/update."""
    real_ip = get_real_ip(request)

    if not x_api_key or len(x_api_key) < 32:
        log.warning("SECURITY_REJECT ip=%s reason=invalid_api_key path=%s", real_ip, request.url.path)
        raise HTTPException(status_code=401, detail="API key inválida")

    client = await get_client_cached(x_api_key)
    if not client:
        log.warning("SECURITY_REJECT ip=%s reason=invalid_api_key path=%s", real_ip, request.url.path)
        raise HTTPException(status_code=401, detail="API key no autorizada")
    if not client["active"]:
        raise HTTPException(status_code=403, detail="Cliente inactivo")

    if not ip_allowed(real_ip, client.get("allowed_ips")):
        log.warning("SECURITY_REJECT ip=%s reason=ip_not_allowed path=%s", real_ip, request.url.path)
        raise HTTPException(status_code=403, detail="IP no autorizada")

    return client


async def require_admin(
    request: Request,
    x_admin_key: str = Header(..., alias="X-Admin-Key"),
) -> None:
    """Dependencia para endpoints internos — nunca se comparte con clientes."""
    if not settings.ADMIN_KEY or x_admin_key != settings.ADMIN_KEY:
        log.warning("SECURITY_REJECT ip=%s reason=invalid_admin_key path=%s", get_real_ip(request), request.url.path)
        raise HTTPException(status_code=403, detail="Acceso denegado")
