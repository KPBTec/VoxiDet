FROM python:3.11-slim

WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY cli/ ./cli/
COPY release.conf .

RUN useradd -m -u 1000 amd && mkdir -p /srv/logs && chown amd:amd /srv/logs
USER amd

EXPOSE 8000

# gunicorn (no uvicorn --workers): con --preload importa app.main UNA vez en
# el proceso padre ANTES de hacer fork de los workers — el modelo Vosk (ver
# app/main.py, cargado a nivel de módulo) queda en memoria compartida por
# copy-on-write entre todos los workers, en vez de una copia completa por
# worker. uvicorn.workers.UvicornWorker: cada worker sigue siendo un proceso
# asyncio/uvicorn normal, gunicorn solo hace de supervisor padre/hijo (además
# reinicia workers que revienten, cosa que uvicorn --workers no hace solo).
# UVICORN_WORKERS lo calcula deploy.sh según CPU/RAM reales del host (ver
# sección "Autotuneo de recursos"). Logging: configurado en código
# (app/main.py, logging.config.dictConfig) — no vía --log-config, que en
# gunicorn espera formato fileConfig/ini, no el dictConfig JSON de este
# proyecto.
CMD gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "${UVICORN_WORKERS:-2}" \
    --preload \
    --bind 0.0.0.0:8000 \
    --timeout 120 \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile -
