#!/bin/bash
# VoxiDet — autotuneo de recursos (CPU/RAM reales + tamaño real del modelo ASR).
#
# Corre en dos momentos:
#   1. Desde deploy.sh, en cada instalación/actualización — deploy.sh ya hace
#      su propio `docker compose up -d --force-recreate` después de este
#      paso, así que acá alcanza con escribir credentials.conf, no hace falta recrear
#      contenedores por separado.
#   2. Desde voxidet-autotune.service (--apply), en cada arranque del sistema
#      — recalcula y, si algo cambió (ej. resize de CPU/RAM + reboot en el
#      proveedor cloud), recrea los contenedores para aplicar los nuevos
#      límites. VoxiDet no mantiene diálogos SIP en vivo (eso lo maneja
#      Asterisk/Kamailio en otros proyectos) — el dialplan ya tiene una ruta
#      de fallback a ERROR si una detección AMD falla a medio camino, así que
#      recrear contenedores acá es de bajo riesgo, a diferencia de reiniciar
#      Kamailio en VoxiKam.
set -euo pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

DEPLOY_DIR="/opt/voxidet"
MODELS_BASE="$DEPLOY_DIR/models-local"

log() { logger -t voxidet-autotune "$*"; echo "[voxidet-autotune] $*"; }

if [[ ! -f "$DEPLOY_DIR/credentials.conf" ]]; then
    log "No existe $DEPLOY_DIR/credentials.conf — nada que autotunear todavía (¿VoxiDet instalado?)"
    exit 0
fi

_set_env() {   # _set_env <archivo> <clave> <valor>
    local file="$1" key="$2" val="$3"
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        # --follow-symlinks: $file es un symlink a $CREDS_FILE (el archivo
        # real vive fuera de $DEPLOY_DIR) — sin este flag, sed -i reemplaza
        # el symlink por un archivo regular nuevo en su lugar, rompiendo el
        # enlace y volviendo a crear dos copias que pueden desincronizarse.
        sed -i --follow-symlinks "s|^${key}=.*|${key}=${val}|" "$file"
    else
        echo "${key}=${val}" >> "$file"
    fi
}

HOST_CPUS=$(nproc)
HOST_RAM_MB=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)
log "Host detectado: ${HOST_CPUS} vCPU, ${HOST_RAM_MB}MB RAM"

# El modelo Vosk se carga UNA sola vez en app/main.py, a nivel de módulo,
# antes del fork de gunicorn (--preload) — todos los workers heredan esas
# páginas por copy-on-write en vez de cargar su propia copia cada uno.
MODEL_SIZE_MB=$(du -sm "$MODELS_BASE" 2>/dev/null | awk '{print $1}')
[[ -z "$MODEL_SIZE_MB" || "$MODEL_SIZE_MB" -eq 0 ]] && MODEL_SIZE_MB=1400
SHARED_MODEL_MB=$(( MODEL_SIZE_MB * 13 / 10 ))
PER_WORKER_MB=250

RESERVED_MB=$(( HOST_RAM_MB / 4 ))
[[ $RESERVED_MB -lt 1024 ]] && RESERVED_MB=1024
AVAILABLE_MB=$(( HOST_RAM_MB - RESERVED_MB - SHARED_MODEL_MB ))
[[ $AVAILABLE_MB -lt $PER_WORKER_MB ]] && AVAILABLE_MB=$PER_WORKER_MB

MAX_WORKERS_RAM=$(( AVAILABLE_MB / PER_WORKER_MB ))
[[ $MAX_WORKERS_RAM -lt 1 ]] && MAX_WORKERS_RAM=1

MAX_WORKERS_CPU=$(( HOST_CPUS > 1 ? HOST_CPUS - 1 : 1 ))

UVICORN_WORKERS=$MAX_WORKERS_CPU
[[ $UVICORN_WORKERS -gt $MAX_WORKERS_RAM ]] && UVICORN_WORKERS=$MAX_WORKERS_RAM
[[ $UVICORN_WORKERS -gt 32 ]] && UVICORN_WORKERS=32
[[ $UVICORN_WORKERS -lt 1  ]] && UVICORN_WORKERS=1

API_CPU_LIMIT=$MAX_WORKERS_CPU
# Margen de seguridad del 20% — sin esto, el límite calculado queda pegado
# exactamente a la estimación (modelo + workers×250MB + 512MB fijo), sin
# nada de colchón para el uso real bajo carga. Confirmado en producción
# (qub-amd, 2026-08-03): con 3 minutos de `docker stats` muestreados cada
# 5s, el contenedor nunca bajó de 93% de uso y tocó 99.9% dos veces —
# a un paso de un OOM-kill que cortaría detecciones AMD en curso — mientras
# el host todavía tenía 3+GB "available" sin usar (`free -h`). El margen
# solo se aplica antes del tope de CAP_MB (que ya reserva RAM para el SO),
# así que nunca empuja al contenedor más allá de lo que el host puede dar.
API_MEM_LIMIT_MB=$(( (SHARED_MODEL_MB + UVICORN_WORKERS * PER_WORKER_MB + 512) * 120 / 100 ))
CAP_MB=$(( HOST_RAM_MB - 512 ))
MIN_NEEDED_MB=$(( SHARED_MODEL_MB + PER_WORKER_MB ))
if [[ $CAP_MB -lt $MIN_NEEDED_MB ]]; then
    log "ALERTA: RAM insuficiente — el modelo ASR necesita ~${MIN_NEEDED_MB}MB y el host solo tiene ${HOST_RAM_MB}MB en total."
    API_MEM_LIMIT_MB=$HOST_RAM_MB
elif [[ $API_MEM_LIMIT_MB -gt $CAP_MB ]]; then
    API_MEM_LIMIT_MB=$CAP_MB
fi

MYSQL_MEM_LIMIT_MB=$(( HOST_RAM_MB / 5 ))
[[ $MYSQL_MEM_LIMIT_MB -lt 256 ]] && MYSQL_MEM_LIMIT_MB=256
MYSQL_BUFFER_POOL_MB=$(( MYSQL_MEM_LIMIT_MB * 6 / 10 ))

# 90 = pool_size(40) + max_overflow(50) por worker, ver app/db/engine.py —
# mantiene MYSQL_MAX_CONNECTIONS como el máximo real que la app puede abrir,
# no un número arbitrario. Si cambias el pool en engine.py, actualiza este *90.
# +20% (no un +20 fijo) para conexiones fuera del pool de uvicorn (CLI/manage.py,
# migraciones, mysqldump, queries directas de admin) — mismo criterio de margen
# proporcional que ya se aplicó a API_MEM_LIMIT_MB arriba, escala con el tamaño
# real del host en vez de quedarse corto en instalaciones grandes con muchos
# workers (un +20 fijo es insignificante frente a 32 workers * 90 = 2880).
MYSQL_MAX_CONNECTIONS=$(( UVICORN_WORKERS * 90 * 120 / 100 ))
[[ $MYSQL_MAX_CONNECTIONS -lt 151 ]] && MYSQL_MAX_CONNECTIONS=151

NOFILE_LIMIT=$(( UVICORN_WORKERS * 8192 ))
[[ $NOFILE_LIMIT -lt 65536 ]] && NOFILE_LIMIT=65536

_OLD_WORKERS=$(grep -m1 '^UVICORN_WORKERS=' "$DEPLOY_DIR/credentials.conf" 2>/dev/null | cut -d= -f2- || true)

_set_env "$DEPLOY_DIR/credentials.conf" UVICORN_WORKERS        "$UVICORN_WORKERS"
_set_env "$DEPLOY_DIR/credentials.conf" API_CPU_LIMIT          "$API_CPU_LIMIT"
_set_env "$DEPLOY_DIR/credentials.conf" API_MEM_LIMIT          "${API_MEM_LIMIT_MB}M"
_set_env "$DEPLOY_DIR/credentials.conf" MYSQL_MEM_LIMIT        "${MYSQL_MEM_LIMIT_MB}M"
_set_env "$DEPLOY_DIR/credentials.conf" MYSQL_BUFFER_POOL      "${MYSQL_BUFFER_POOL_MB}M"
_set_env "$DEPLOY_DIR/credentials.conf" MYSQL_MAX_CONNECTIONS  "$MYSQL_MAX_CONNECTIONS"
_set_env "$DEPLOY_DIR/credentials.conf" NOFILE_LIMIT           "$NOFILE_LIMIT"

log "Workers: $UVICORN_WORKERS | API mem: ${API_MEM_LIMIT_MB}MB | MySQL mem: ${MYSQL_MEM_LIMIT_MB}MB (buffer pool ${MYSQL_BUFFER_POOL_MB}MB, max_connections ${MYSQL_MAX_CONNECTIONS}) | nofile: $NOFILE_LIMIT"

if [[ "$APPLY" == "1" ]]; then
    if [[ "$_OLD_WORKERS" != "$UVICORN_WORKERS" ]]; then
        log "Recursos cambiaron (workers ${_OLD_WORKERS:-?} → ${UVICORN_WORKERS}) — recreando contenedores..."
        (cd "$DEPLOY_DIR/docker" && docker compose --env-file "$DEPLOY_DIR/credentials.conf" up -d --force-recreate)
        sleep 5
        if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
            log "Contenedores recreados OK — /health responde"
        else
            log "ALERTA: /health no responde tras recrear contenedores — revisar: docker compose logs api"
        fi
    else
        log "Sin cambios respecto al último cálculo — no hace falta recrear contenedores"
    fi
fi
