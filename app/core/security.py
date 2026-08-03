"""
Middleware de seguridad en la capa de aplicación.
Complementa nftables (capa de red) con controles en la app:
  - Rate limiting por IP con ventana deslizante
  - Bloqueo de User-Agents maliciosos conocidos
  - Security headers en todas las respuestas
"""
import logging
import time
from typing import Callable

from cachetools import TTLCache
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("voxidet.security")

RATE_LIMITS = {
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
# necesita ningún host externo.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options":        "DENY",
    "X-XSS-Protection":       "1; mode=block",
    "Referrer-Policy":        "strict-origin-when-cross-origin",
    "Permissions-Policy":     "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data:; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "connect-src 'self';"
    ),
    "Server": "VoxiDet",
}

# Antes: dict[str, list[float]] con key f"{ip}:{prefix}" — se filtraban los
# timestamps vencidos pero la key nunca se borraba con la lista vacía, así
# que el dict crecía sin límite durante la vida del proceso (una entrada
# fantasma por cada IP distinta que alguna vez pegó contra /amd o /admin/).
# TTLCache por prefijo resuelve ambos problemas: una IP inactiva por más de
# `window` segundos se auto-expira sola (sin tarea de limpieza aparte), y
# `maxsize` pone un techo duro para el caso extremo de un burst con miles de
# IPs distintas en la misma ventana, antes de que el TTL llegue a actuar.
_counters: dict[str, TTLCache] = {
    prefix: TTLCache(maxsize=50_000, ttl=window)
    for prefix, (_, window) in RATE_LIMITS.items()
}


def _get_ip(request: Request) -> str:
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip().split(",")[0].strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        ua   = request.headers.get("user-agent", "").lower()
        ip   = _get_ip(request)

        # Bloquear scanners conocidos
        if any(b in ua for b in BLOCKED_UAS):
            log.warning("SECURITY_REJECT ip=%s reason=blocked_ua path=%s", ip, path)
            return JSONResponse({"detail": "Forbidden"}, status_code=403)

        # Rate limiting por prefijo
        now = time.monotonic()
        for prefix, (max_req, window) in RATE_LIMITS.items():
            if path.startswith(prefix):
                cache = _counters[prefix]
                hits  = [t for t in cache.get(ip, ()) if now - t < window]
                if len(hits) >= max_req:
                    # No se loguea como SECURITY_REJECT (no alimenta fail2ban): un
                    # dialer legítimo de alto volumen puede superar el límite en
                    # tráfico normal — banearlo por esto sería un auto-DoS.
                    return JSONResponse(
                        {"detail": "Rate limit — intenta mas tarde"},
                        status_code=429,
                        headers={"Retry-After": str(window)},
                    )
                hits.append(now)
                cache[ip] = hits   # reinserta → refresca el TTL de esta IP
                break

        response = await call_next(request)

        # Security headers
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value

        return response
