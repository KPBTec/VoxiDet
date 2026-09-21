FROM python:3.11-slim

WORKDIR /srv

# upgrade -y antes de instalar: python:3.11-slim es un tag flotante, los
# paquetes de sistema ya horneados en la imagen base no se actualizan solos
# — sin esto, un CVE de Debian parcheado después de la última vez que se
# publicó esa imagen base queda sin corregir en cada build nuevo.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    libgomp1 \
    libjemalloc2 \
    && rm -rf /var/lib/apt/lists/*

# jemalloc reemplaza el malloc() de glibc para todo el proceso: fragmenta
# mucho menos que glibc en cargas Python+numérico multi-hilo (numpy/
# ONNXRuntime/PyTorch), que es exactamente el patrón de cada worker acá.
# Medido en producción (ver CHANGELOG.md v1.28.2/v1.28.3).
#
# IMPORTANTE — una vez que jemalloc queda activo por LD_PRELOAD, glibc deja
# de manejar las asignaciones reales del proceso. Esto vuelve inútiles dos
# mecanismos glibc-específicos que se habían probado antes: MALLOC_ARENA_MAX
# (glibc ignora ese env var una vez que jemalloc tiene el malloc()) y
# malloc_trim() llamado manualmente (verificado con dlsym: el símbolo resuelve
# a la implementación de glibc, que queda operando sobre un heap vacío ya que
# jemalloc nunca lo usa — sería un no-op silencioso). Se reemplazan ambos por
# la configuración NATIVA de jemalloc via MALLOC_CONF:
#   - narenas:2     → mismo objetivo que tenía MALLOC_ARENA_MAX=2 (menos
#                     arenas = menos memoria de sobra reservada por hilo),
#                     pero en el sistema que realmente maneja la memoria.
#   - dirty_decay_ms:1000 / muzzy_decay_ms:1000 → jemalloc ya purga solo
#                     páginas libres de vuelta al SO (default: cada 10s);
#                     se baja a 1s porque el patrón acá es de picos cortos
#                     por llamada (buffers de audio + tensores de inferencia)
#                     seguidos de inactividad — conviene devolver esa memoria
#                     rápido en vez de esperar 10s "por si se reusa".
ENV LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2
ENV MALLOC_CONF=narenas:2,dirty_decay_ms:1000,muzzy_decay_ms:1000

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
