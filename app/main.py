"""
VoxiDet — entry point.
Solo responsabilidades: crear la app, registrar el router y aplicar middleware.
La lógica de negocio vive en core/, los datos en db/, la HTTP en api/.
"""

import json
import logging
import logging.config
import pathlib
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.api.router import router
from app.core.security import SecurityMiddleware
from app.db.migrations import run_pending_migrations
from app.db.providers import load_all_models_to_cache
from app.core import keyword_cache
from app.core import usage_sync
from app.core.local_asr import init_vosk, init_sherpa, init_sherpa_large, discover_models
from app.core.silero_vad import init_silero_vad
from app.core.http_client import close_http_client
from app.core.audiosocket_server import create_listening_socket, start_audiosocket_server

_APP_DIR = pathlib.Path(__file__).parent

# Logging configurado en código, no vía flag --log-config del proceso que
# arranca la app: bajo gunicorn --preload esto corre en el proceso padre
# antes del fork (ver más abajo), y gunicorn no entiende el dictConfig JSON
# que ya usa este proyecto (su --log-config espera formato fileConfig/ini).
with open(_APP_DIR / "log_config.json") as _f:
    logging.config.dictConfig(json.load(_f))

log = logging.getLogger("voxidet.main")

# ── Modelos locales ASR — cargados UNA sola vez aquí, a nivel de módulo ──────
# No van en lifespan() a propósito: lifespan corre DENTRO de cada worker
# (después del fork), así que si el modelo se cargara ahí, cada worker
# terminaría con su propia copia completa en RAM (~1.4GB el modelo Vosk
# español, ×N workers). Cargándolo aquí — antes del fork, bajo gunicorn
# --preload — los workers heredan las mismas páginas de memoria por
# copy-on-write: el modelo se paga UNA vez sin importar cuántos workers
# corran. Verificado empíricamente (fork + páginas de solo lectura +
# medición de Pss real vs Rss) antes de aplicar este cambio.
# discover_models() no requiere config en credentials.conf — deploy.sh escribe los
# .version al instalar los modelos en MODELS_BASE.
_models = discover_models(settings.MODELS_BASE)
if _models["vosk"]:
    init_vosk(_models["vosk"])
if _models["sherpa"]:
    init_sherpa(_models["sherpa"])
if _models["sherpa_large"]:
    init_sherpa_large(_models["sherpa_large"])
if _models["silero"]:
    init_silero_vad(_models["silero"])

# AudioSocket (v1.27.0): bind+listen ANTES del fork, mismo motivo que los
# modelos de arriba — bajo gunicorn --preload, todos los workers heredan el
# mismo socket ya escuchando por copy-on-write. Si cada worker intentara
# bindear el puerto por su cuenta en lifespan() (que corre DESPUÉS del fork,
# dentro de cada worker), solo el primero lo conseguiría — "Address already
# in use" en los otros 10.
# Envuelto en try/except a propósito: este bind corre en el proceso padre de
# gunicorn --preload, junto con la carga de los modelos ASR de arriba — un
# OSError sin atrapar acá (puerto ya ocupado, AUDIOSOCKET_PORT mal configurado
# chocando con otro puerto del mismo proceso, etc.) tumbaría el import de todo
# app.main y con eso el arranque de gunicorn completo, afectando también los
# modos batch/stream que no dependen de AudioSocket para nada.
_audiosocket_sock = None
try:
    _audiosocket_sock = create_listening_socket()
except OSError as e:
    log.error(
        "AudioSocket: no se pudo bindear 0.0.0.0:%d (%s) — el modo AudioSocket "
        "queda deshabilitado en este proceso; batch y stream siguen funcionando normalmente.",
        settings.AUDIOSOCKET_PORT, e,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("VoxiDet iniciando... panel en %s", settings.ADMIN_PREFIX)
    await run_pending_migrations()
    await load_all_models_to_cache()
    await keyword_cache.start()
    await usage_sync.start()
    from app.api.stream import _log_groq_keys
    await _log_groq_keys()
    audiosocket_server = await start_audiosocket_server(_audiosocket_sock) if _audiosocket_sock is not None else None
    yield
    if audiosocket_server is not None:
        audiosocket_server.close()
        await audiosocket_server.wait_closed()
    await close_http_client()
    log.info("VoxiDet detenido.")


app = FastAPI(
    title    = "VoxiDet",
    version  = "1.0.0",
    docs_url = None,
    redoc_url= None,
    lifespan = lifespan,
)


app.add_middleware(SecurityMiddleware)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start    = time.monotonic()
    response = await call_next(request)
    elapsed  = int((time.monotonic() - start) * 1000)
    log.debug("%s %s %dms", request.method, request.url.path, elapsed)
    return response


app.mount("/static", StaticFiles(directory=str(_APP_DIR / "static")), name="static")
app.include_router(router)
