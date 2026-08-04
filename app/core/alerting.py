"""
Notificaciones proactivas — hasta ahora nadie se enteraba de un proveedor
ASR caído o un cliente golpeando su límite diario repetidamente hasta
revisar los logs al día siguiente. Opt-in: sin ALERT_WEBHOOK_URL configurado
en credentials.conf, notify() es un no-op (mismo patrón que
INSTALL_SHERPA_LARGE — no aparece ni se usa por accidente si nadie lo
configuró a propósito).

Acepta cualquier webhook que reciba POST {"text": "..."} — Slack incoming
webhook, n8n, Discord (con /slack al final de la URL), o un endpoint propio.
"""
import logging

from app.config import settings
from app.core.http_client import get_http_client

log = logging.getLogger("voxidet.alerting")

_DEDUP_TTL = 300  # no repetir la misma alerta (mismo `event`) dentro de 5 minutos


async def notify(event: str, detail: str) -> None:
    """event: clave corta y estable (ej. 'proveedor_caido:groq',
    'limite_diario:123') — se usa para el dedup, no para el texto mostrado."""
    if not settings.ALERT_WEBHOOK_URL:
        return

    try:
        from app.cache.client_cache import get_redis
        r = await get_redis()
        dedup_key = f"amd:alert:{event}"
        if await r.get(dedup_key):
            return  # mismo evento ya alertado hace poco — no repetir
        await r.set(dedup_key, "1", ex=_DEDUP_TTL)
    except Exception:
        pass  # si Redis falla, mejor mandar de más que perder la alerta

    try:
        client = get_http_client()
        await client.post(
            settings.ALERT_WEBHOOK_URL,
            json={"text": f"⚠️ VoxiDet — {detail}"},
            timeout=5.0,
        )
    except Exception as e:
        log.warning("No se pudo enviar la alerta '%s': %s", event, e)
