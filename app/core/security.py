"""
Middleware de seguridad en la capa de aplicación.
Complementa nftables (capa de red) con controles en la app:
  - Rate limiting por IP con ventana deslizante
  - Bloqueo de User-Agents maliciosos conocidos
  - Security headers en todas las respuestas
"""
import logging
import secrets
import time
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("voxidet.security")

RATE_LIMITS = {
    "/amd/stream":    (30,  60),   # 30 conexiones nuevas/min por IP — antes del límite general de /amd
    "/amd":           (120, 60),   # 120 req/min por IP en endpoints AMD
    "/admin/":        (60,  60),   # 60 req/min en panel admin
}

BLOCKED_UAS = {
    "sqlmap", "nikto", "masscan", "nmap", "zgrab",
    "dirbuster", "gobuster", "wfuzz", "hydra", "nuclei",
    "python-httpx", "go-http-client",
}

# CSP cubre el panel admin real (server-rendered), no solo JSON como en
# VoxiKam — por eso permite Google Fonts (style-src/font-src). Chart.js está
# vendorizado en /static/vendor/ (ya no jsdelivr), así que script-src no
# necesita ningún host externo salvo el de abajo.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options":        "DENY",
    "X-XSS-Protection":       "1; mode=block",
    "Referrer-Policy":        "strict-origin-when-cross-origin",
    "Permissions-Policy":     "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        # static.cloudflareinsights.com: Cloudflare inyecta este beacon solo
        # cuando el dominio pasa por su proxy con Web Analytics activado en
        # el dashboard de Cloudflare — no es algo que VoxiDet agregue, pero
        # sin este host en script-src el propio CSP lo bloquea y ensucia la
        # consola con violaciones en cada carga de página.
        "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data:; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "connect-src 'self' https://cloudflareinsights.com;"
    ),
    "Server": "VoxiDet",
}

def get_ip(request: Request) -> str:
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip().split(",")[0].strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_blocked_ua(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    return any(b in ua for b in BLOCKED_UAS)


async def check_rate_limit(ip: str, path: str) -> tuple[bool, int]:
    """(excede_limite, ventana_en_segundos) para el primer prefijo de
    RATE_LIMITS que matchee `path`. No se loguea como SECURITY_REJECT (no
    alimenta fail2ban) — un dialer legítimo de alto volumen puede superar el
    límite en tráfico normal, banearlo por esto sería un auto-DoS. Reusado
    tanto por el middleware HTTP (dispatch) como por el WebSocket de stream,
    que BaseHTTPMiddleware no puede proteger (ver amd_stream() en stream.py).

    Contador en Redis (sorted set, ventana deslizante), no en memoria del
    proceso — encontrado en producción (mismo patrón que
    local_asr.py::_SHERPA_CONCURRENCY): un TTLCache normal vive DENTRO de
    cada worker de gunicorn, no se comparte entre ellos. Con N workers, el
    límite real efectivo terminaba siendo el configurado multiplicado por N
    (una IP abusiva repartida entre 11 workers por el balanceo de conexiones
    veía, en la práctica, ~11x el límite anunciado antes de que cualquier
    worker individual la bloqueara). Redis lo comparte de verdad entre todos.
    Fail-open si Redis está caído — no cortar tráfico legítimo por eso, mismo
    criterio que el circuit breaker de proveedores (amd_engine.py)."""
    for prefix, (max_req, window) in RATE_LIMITS.items():
        if path.startswith(prefix):
            try:
                from app.cache.client_cache import get_redis
                r = await get_redis()
                key = f"amd:ratelimit:{prefix}:{ip}"
                # time.time() (reloj de pared), no time.monotonic() — el
                # monotonic de cada proceso tiene su propio origen arbitrario,
                # no es comparable entre workers distintos.
                now = time.time()
                await r.zremrangebyscore(key, 0, now - window)
                count = await r.zcard(key)
                if count >= max_req:
                    return True, window
                await r.zadd(key, {f"{now}:{secrets.token_hex(3)}": now})
                await r.expire(key, window)
                return False, window
            except Exception as e:
                log.warning("check_rate_limit: Redis no disponible (%s) — fail-open", e)
                return False, window
    return False, 0


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        ua   = request.headers.get("user-agent", "")
        ip   = get_ip(request)

        # Bloquear scanners conocidos
        if is_blocked_ua(ua):
            log.warning("SECURITY_REJECT ip=%s reason=blocked_ua path=%s", ip, path)
            return JSONResponse({"detail": "Forbidden"}, status_code=403)

        # Rate limiting por prefijo — BaseHTTPMiddleware solo procesa scope
        # "http", así que esto nunca corre para el WebSocket de /amd/stream
        # (protegido aparte, directo en amd_stream(), ver check_rate_limit()).
        exceeded, window = await check_rate_limit(ip, path)
        if exceeded:
            return JSONResponse(
                {"detail": "Rate limit — intenta mas tarde"},
                status_code=429,
                headers={"Retry-After": str(window)},
            )

        response = await call_next(request)

        # Security headers
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value

        return response
