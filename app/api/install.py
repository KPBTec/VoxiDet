import hashlib
import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.db.clients import get_client_by_install_token
from app.core.agi_template import TEMPLATE

_AGI_VERSION = hashlib.sha256(TEMPLATE.encode()).hexdigest()[:12]

log = logging.getLogger("voxidet.install")
router = APIRouter()

_PLACEHOLDER = {"IP_VPS", "localhost", "tudominio"}


def _resolve_server_url(request: Request) -> str:
    """
    Devuelve la URL base del servidor.
    Prioridad: PUBLIC_URL configurado → URL del request actual.
    Si PUBLIC_URL tiene valor de ejemplo, usa el request (auto-detect).
    """
    pub = (settings.PUBLIC_URL or "").rstrip("/")
    if pub and not any(p in pub for p in _PLACEHOLDER):
        # Sin esquema (ej. PUBLIC_URL=10.100.10.15:8000 en credentials.conf,
        # sin "http://") esto se horneaba tal cual en el AGI descargado —
        # urlopen() del lado del cliente falla con "unknown url type" en
        # TODAS las llamadas HTTP del script (check/update/batch), en
        # silencio para /amd/check (que traga la excepción y cae a modo
        # batch por default) y visible recién en el propio batch.
        if "://" not in pub:
            pub = "http://" + pub
        return pub
    # Auto-detect desde el request (funciona con IP directa o dominio)
    return f"{request.url.scheme}://{request.url.netloc}"


@router.get("/install/{install_token}")
async def install_agi(install_token: str, request: Request):
    """
    Devuelve el AGI pre-configurado con servidor y API key incrustados.
    La URL del servidor se auto-detecta si PUBLIC_URL no está configurado.

    En cada nodo Asterisk:
        wget http://IP_VPS:8000/install/TOKEN -O /var/lib/asterisk/agi-bin/amd_ia.agi
        chmod 755 /var/lib/asterisk/agi-bin/amd_ia.agi
    """
    if not install_token or len(install_token) < 32:
        raise HTTPException(status_code=401, detail="Token inválido")

    client = await get_client_by_install_token(install_token)
    if not client or not client["active"]:
        raise HTTPException(status_code=401, detail="Token inválido o cliente inactivo")

    server_url = _resolve_server_url(request)
    script = (TEMPLATE
        .replace("__SERVER__",  server_url)
        .replace("__APIKEY__",  client["api_key"])
        .replace("__CLIENT__",  client["name"])
        .replace("__VERSION__", _AGI_VERSION)
    )

    log.info("AGI descargado — cliente id=%s (%s) servidor=%s", client["id"], client["name"], server_url)
    return PlainTextResponse(
        content=script,
        headers={"Content-Disposition": "attachment; filename=amd_ia.agi"},
    )
