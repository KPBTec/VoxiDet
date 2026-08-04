#!/usr/bin/env bash
# deploy.sh — Despliega VoxiDet en /opt/voxidet
#
# VoxiDet — Voice Detection AI. Un desarrollo de KPBTec.
#
# Uso:
#   git clone <repo> && cd voxidet
#   sudo bash deploy.sh          ← primera vez: pregunta datos y genera credentials.conf solo
#   sudo bash deploy.sh          ← instalación existente: muestra versión instalada vs
#                                   la del repo y pide confirmar antes de actualizar
#   sudo bash deploy.sh --yes    ← salta la confirmación (automatización/CI)
#
# Marcador del sistema: /etc/voxidet.conf (mismo patrón que VoxiKam, /etc/voxikam.conf)
#
set -euo pipefail

INSTALL_START=$SECONDS   # timer global — se muestra en el resumen final
ALL_OK=true               # si el healthcheck falla, pasa a false — mismo patrón que VoxiKam

DEPLOY_DIR="/opt/voxidet"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
MARKER_FILE="/etc/voxidet.conf"   # ubicación fija — mismo patrón que VoxiKam (/etc/voxikam.conf)

# --yes/-y: saltea el menú de confirmación (para automatización/CI) — sin
# flags, siempre interactivo cuando ya hay una instalación previa.
_SKIP_CONFIRM=0
for _arg in "$@"; do
    case "$_arg" in
        --yes|-y) _SKIP_CONFIRM=1 ;;
    esac
done

# Versión del release (release.conf — única fuente, también la lee el panel admin)
PLATFORM_VERSION="0.0.0"
PLATFORM_NAME="VoxiDet"
[[ -f "$SRC_DIR/release.conf" ]] && source "$SRC_DIR/release.conf"

# Log de la corrida completa — patrón compartido con VoxiKam. Sin esto, un
# fallo a mitad de deploy (como el de la migración amd_keywords) solo vive
# en el scrollback de la terminal y se pierde apenas se cierra la sesión.
LOG_DIR="/var/log/voxidet-deploy"
mkdir -p "$LOG_DIR" 2>/dev/null || true
if [[ -w "$LOG_DIR" ]]; then
    LOG_FILE="$LOG_DIR/deploy-$(date +%Y%m%d-%H%M%S).log"
    exec > >(tee -a "$LOG_FILE") 2>&1
    echo "Log completo de esta corrida: $LOG_FILE"
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info() { echo -e "${BLUE}[·]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[⚠]${NC} $*"; }
die()  { echo -e "\n${RED}[✗] $*${NC}\n" >&2; exit 1; }
sep()  { echo -e "\n${BOLD}${BLUE}── $* ──────────────────────────────────────────────────────────${NC}"; }

[[ "$(id -u)" -eq 0 ]] || die "Ejecuta como root:  sudo bash deploy.sh"

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  ${PLATFORM_NAME:-VoxiDet} v${PLATFORM_VERSION} — un desarrollo de KPBTec${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "  ${BOLD}Directorio origen:${NC} $SRC_DIR"
echo -e "  ${BOLD}Usuario:${NC}           voxidet  (sin shell, sin login — solo servicios)"
echo -e "  ${BOLD}Log:${NC}               ${LOG_FILE:-(sin permisos de escritura en $LOG_DIR)}"
echo ""

# ─── Instalación existente — comparar versión y confirmar antes de proceder ──
# Mismo patrón que VoxiKam (marcador en /etc, comparación de versión, menú
# antes de tocar nada) — antes VoxiDet actualizaba de frente sin avisar qué
# versión había ni pedir confirmación.
if [[ -f "$MARKER_FILE" ]]; then
    _INSTALLED_VERSION=$(grep "^VERSION=" "$MARKER_FILE" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || echo "desconocida")
    _INSTALLED_DATE=$(grep "^INSTALL_DATE=" "$MARKER_FILE" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || echo "")

    sep "Instalación existente detectada"
    echo -e "  ${BOLD}Versión instalada:${NC}  v${_INSTALLED_VERSION:-?}"
    echo -e "  ${BOLD}Versión en repo:${NC}    v${PLATFORM_VERSION}"
    [[ -n "$_INSTALLED_DATE" ]] && echo -e "  ${BOLD}Instalado el:${NC}      $(echo "$_INSTALLED_DATE" | cut -dT -f1)"
    echo ""

    if [[ "$_INSTALLED_VERSION" != "$PLATFORM_VERSION" ]]; then
        echo -e "  Hay una versión nueva disponible (v${_INSTALLED_VERSION} → v${PLATFORM_VERSION})."
    else
        echo -e "  Ya tienes la versión más reciente (v${PLATFORM_VERSION})."
    fi
    echo ""
    warn "Este deploy recrea los contenedores (docker compose up -d --force-recreate) — corta momentáneamente las detecciones AMD en curso en el momento del restart."
    echo ""

    if [[ "$_SKIP_CONFIRM" -eq 0 ]]; then
        echo -e "  ${BOLD}1)${NC} Actualizar — código + migraciones + recrea contenedores"
        echo -e "  ${BOLD}2)${NC} Cancelar"
        echo ""
        read -r -p "  Opción [1/2]: " _OPT
        case "${_OPT:-2}" in
            1) : ;;
            *) info "Cancelado."; exit 0 ;;
        esac
        echo ""
    fi
fi

# Helpers de input — mismo patrón que VoxiKam: primera corrida pregunta con
# defaults auto-detectados/generados (ENTER acepta, o se edita ahí mismo),
# corridas siguientes ya no preguntan nada (credentials.conf existente se reusa/ajusta).
ask() {
    local txt="$1" def="$2" var="$3" val=""
    if [[ -n "$def" ]]; then
        read -r -p "  $txt [$def]: " val
        printf -v "$var" "%s" "${val:-$def}"
    else
        while [[ -z "$val" ]]; do read -r -p "  $txt (requerido): " val; done
        printf -v "$var" "%s" "$val"
    fi
}
ask_secret() {
    local txt="$1" var="$2" v1="" v2=""
    while true; do
        read -r -s -p "  $txt: " v1; echo ""
        read -r -s -p "  Confirmar: " v2; echo ""
        [[ "$v1" == "$v2" && ${#v1} -ge 8 ]] && { printf -v "$var" "%s" "$v1"; break; }
        [[ ${#v1} -lt 8 ]] && echo -e "  ${YELLOW}Mínimo 8 caracteres.${NC}" || echo -e "  ${YELLOW}No coinciden.${NC}"
    done
}
ask_yn() {
    # Pregunta s/N — con --yes/-y no pregunta y usa el default (nunca activa
    # opciones pesadas/lentas solo por automatizar el deploy).
    local txt="$1" def="$2" var="$3" val=""
    if [[ "$_SKIP_CONFIRM" -eq 1 ]]; then
        printf -v "$var" "%s" "$def"
        return
    fi
    read -r -p "  $txt [$([[ "$def" == "true" ]] && echo "S/n" || echo "s/N")]: " val
    if [[ -z "$val" ]]; then
        printf -v "$var" "%s" "$def"
    else
        case "$val" in
            s|S|si|Si|SI|sí|Sí|SÍ|y|Y|yes|Yes) printf -v "$var" "true" ;;
            *) printf -v "$var" "false" ;;
        esac
    fi
}

# ─── 0. Migración automática de una instalación anterior (vdamd → voxidet) ───
# Detecta el deploy viejo (anterior al rename amd_*/vdamd → voxidet) y, si
# existe y todavía no se migró, respalda sus datos y baja su stack ANTES de
# que el resto del script intente levantar el nuevo (mismos puertos 3306/8000).
MIGRATE_PENDING=0
MIGRATE_DUMP="/root/amd_data_backup.sql"
if [[ -d /opt/vdamd && ! -d "$DEPLOY_DIR" ]] && command -v docker &>/dev/null; then
    sep "Migrando instalación anterior (vdamd → voxidet)"
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'amd_mysql'; then
        OLD_ROOT_PW=$(grep -m1 '^MYSQL_ROOT_PASSWORD=' /opt/vdamd/.env | cut -d= -f2- || true)
        info "Respaldando amd_db (instalación anterior)..."
        docker exec amd_mysql mysqldump -u root -p"$OLD_ROOT_PW" --no-create-info amd_db > "$MIGRATE_DUMP"
        LINES=$(wc -l < "$MIGRATE_DUMP")
        [[ "$LINES" -gt 10 ]] || die "El dump de amd_db se ve vacío ($LINES líneas) — abortando antes de tocar nada"
        sed -i \
            -e 's/`amd_logs`/`voxidet_logs`/g' \
            -e 's/`amd_keywords`/`voxidet_keywords`/g' \
            "$MIGRATE_DUMP"
        ok "Backup OK ($LINES líneas) en $MIGRATE_DUMP"

        info "Deteniendo stack anterior (sin borrar volúmenes)..."
        (cd /opt/vdamd && docker compose down)
        ok "Stack anterior detenido — /opt/vdamd y sus volúmenes quedan intactos"
        MIGRATE_PENDING=1
    else
        info "amd_mysql no está corriendo — nada que respaldar, continuo con deploy normal"
    fi
fi

# ─── 1. Credenciales — mismo patrón que VoxiKam: un solo archivo real fuera
# del repo (/voxidet-install/logs-configs/), con symlink visible en el
# proyecto. Formato KEY=VALOR sin cambios — Docker Compose y pydantic leen
# el archivo directo, un formato [sección] los rompería.
sep "Verificando credenciales"

CREDS_DIR="/voxidet-install/logs-configs"
CREDS_FILE="$CREDS_DIR/credentials.conf"
ENV_FILE="$CREDS_FILE"   # alias — el resto del script usa este nombre

# ─── Migración automática: .env viejo (raíz del repo o $DEPLOY_DIR) → la
# nueva ubicación fuera del repo. Sin esto, un servidor YA instalado sería
# tratado como "primera instalación": generaría contraseñas nuevas al azar
# que no coinciden con las que MySQL ya tiene guardadas en su volumen,
# rompiendo la conexión a la base de datos existente. Se reusa tal cual —
# nada se regenera. $DEPLOY_DIR/.env se revisa primero por ser la copia que
# Docker Compose usa de verdad ahora mismo (más autoritativa que $SRC_DIR/.env
# si alguna vez llegaron a divergir).
if [[ ! -f "$CREDS_FILE" ]]; then
    _LEGACY_ENV=""
    [[ -f "$DEPLOY_DIR/.env" ]] && _LEGACY_ENV="$DEPLOY_DIR/.env"
    [[ -z "$_LEGACY_ENV" && -f "$SRC_DIR/.env" ]] && _LEGACY_ENV="$SRC_DIR/.env"

    if [[ -n "$_LEGACY_ENV" ]]; then
        echo ""
        echo -e "  ${YELLOW}Instalación existente detectada — migrando $_LEGACY_ENV → $CREDS_FILE${NC}"
        echo -e "  ${YELLOW}(se reusan las credenciales tal cual, no se genera nada nuevo)${NC}"
        echo ""
        mkdir -p "$CREDS_DIR"; chmod 700 "$CREDS_DIR"
        cp "$_LEGACY_ENV" "$CREDS_FILE"
        chmod 600 "$CREDS_FILE"
        ok "Credenciales existentes migradas a $CREDS_FILE"
    fi
fi

if [[ ! -f "$CREDS_FILE" ]]; then
    echo ""
    echo -e "  ${YELLOW}No existen credenciales en $CREDS_FILE — primera instalación${NC}"
    echo ""
    info "Detectando IP pública..."
    DETECTED_PUBLIC=""
    for svc in "https://api.ipify.org" "https://ifconfig.me" "https://icanhazip.com"; do
        DETECTED_PUBLIC=$(curl -s --max-time 4 "$svc" 2>/dev/null | tr -d '[:space:]')
        [[ -n "$DETECTED_PUBLIC" ]] && break
    done
    [[ -n "$DETECTED_PUBLIC" ]] && ok "IP pública: $DETECTED_PUBLIC" \
        || echo -e "  ${YELLOW}No se detectó IP pública — completa PUBLIC_URL a mano.${NC}"
    echo ""
    echo "  Presiona ENTER para aceptar el valor detectado/sugerido."
    echo ""

    ask "URL pública (el AGI la usa para bajar el agente)" "http://${DETECTED_PUBLIC:-IP_VPS}:8000" PUBLIC_URL
    ask "Usuario admin (panel CMS)" "admin" ADMIN_USER
    ask_secret "Password admin (mín. 8 chars)" ADMIN_PASSWORD
    echo ""
    ask_yn "¿Instalar también Sherpa Whisper Large (whisper-large-v3 completo, ~1GB, más preciso pero varios segundos por clip en CPU — nunca se usa como fallback automático)?" "false" INSTALL_SHERPA_LARGE

    MYSQL_ROOT_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 28)
    MYSQL_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 28)
    REDIS_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 28)
    ADMIN_KEY=$(openssl rand -base64 32 | tr -d '/+=')
    SECRET_KEY=$(openssl rand -hex 32)
    # Fernet key (32 bytes random, base64 url-safe) — cifrado en reposo de API
    # keys guardadas desde el panel (v1.16.0). No usar openssl rand -base64 solo:
    # el alfabeto estándar (+/) no es el url-safe que espera Fernet, hay que
    # traducirlo. Verificado que decodifica bien con cryptography.fernet.Fernet.
    KEYS_ENCRYPTION_SECRET=$(openssl rand 32 | openssl base64 -A | tr '+/' '-_')

    # Heredoc directo — mismo mecanismo que VoxiKam (antes: copiar
    # credentials.conf.example + sed sobre 8 claves puntuales, dependía de un
    # archivo de plantilla en el repo). MYSQL_DATABASE no va acá: está fijo en
    # docker-compose.yml (voxidet_db), no es configurable.
    mkdir -p "$CREDS_DIR"; chmod 700 "$CREDS_DIR"
    cat > "$CREDS_FILE" <<EOF
# VoxiDet — Credenciales
# Generado: $(date)
# MANTENER SEGURO — NO COMPARTIR

# MySQL
MYSQL_ROOT_PASSWORD=$MYSQL_ROOT_PASSWORD
MYSQL_USER=voxidet_user
MYSQL_PASSWORD=$MYSQL_PASSWORD

# Redis
REDIS_PASSWORD=$REDIS_PASSWORD

# ── Proveedores de transcripción ─────────────────────────────────────────────
# Usa keys numeradas (_1, _2, _3...) para múltiples cuentas por proveedor.
# El sistema las rota automáticamente para distribuir la carga.

# Groq Whisper — gratis: 20 RPM/key · console.groq.com → API Keys
GROQ_API_KEY_1=

# Deepgram Nova — batch (v1) y streaming en tiempo real (v2)
DEEPGRAM_API_KEY_1=

# Fireworks AI — Whisper sin límite RPM
FIREWORKS_API_KEY_1=

# Together AI — cobra por segundo real (sin mínimo 10s)
TOGETHER_API_KEY_1=

# OpenAI Transcribe — gpt-4o-mini-transcribe
OPENAI_API_KEY_1=

# ── App ───────────────────────────────────────────────────────────────────────
# URL que se incrusta en el AGI descargado por los nodos Asterisk.
PUBLIC_URL=$PUBLIC_URL

# Clave para endpoints internos — nunca compartir con clientes
ADMIN_KEY=$ADMIN_KEY

# Path del panel admin — cambiar en producción, nunca dejar /admin
ADMIN_PREFIX=/admin

# Credenciales del CMS
ADMIN_USER=$ADMIN_USER
ADMIN_PASSWORD=$ADMIN_PASSWORD

# Clave para firmar cookies de sesión del CMS
SECRET_KEY=$SECRET_KEY

# Clave de cifrado (Fernet) para API keys guardadas desde el panel — NO
# regenerar en instalaciones existentes, se perdería el acceso a las keys
# ya guardadas en la base de datos.
KEYS_ENCRYPTION_SECRET=$KEYS_ENCRYPTION_SECRET

# Detección de tono de beep de buzón (experimental, solo logging)
AMD_BEEP_FREQ_HZ=1000

# Whisper large-v3 completo local (Sherpa-onnx) — opt-in, ~1GB de descarga y
# varios segundos por clip en CPU (no recomendado para el camino automático
# de detección). true para que deploy.sh lo descargue.
INSTALL_SHERPA_LARGE=$INSTALL_SHERPA_LARGE

# ── Autotuneo de recursos (NO editar a mano) ─────────────────────────────────
# deploy.sh calcula estos valores en cada corrida según CPU/RAM reales del
# host — se sobrescriben solos, dejarlos vacíos.
UVICORN_WORKERS=
API_CPU_LIMIT=
API_MEM_LIMIT=
MYSQL_MEM_LIMIT=
MYSQL_BUFFER_POOL=
MYSQL_MAX_CONNECTIONS=
NOFILE_LIMIT=
EOF
    chmod 600 "$CREDS_FILE"

    echo ""
    ok "credentials.conf generado en $CREDS_FILE con las credenciales de arriba"
    echo -e "  ${YELLOW}Las API keys de transcripción (GROQ/DEEPGRAM/FIREWORKS/TOGETHER/OPENAI) quedan vacías${NC}"
    echo -e "  ${YELLOW}— opcional, edítalas luego en $CREDS_FILE si las vas a usar.${NC}"
    echo ""
fi

# Sin symlink en $SRC_DIR (la carpeta del repo clonado) — VoxiKam tampoco lo
# hace en su $SCRIPT_DIR, solo enlaza dentro de su directorio de instalación
# ($INSTALL_DIR). Mismo criterio acá: nada de credentials.conf en la raíz
# del repo, ni siquiera como symlink — solo vive dentro de $DEPLOY_DIR.

# Validar que no queden valores de ejemplo sin cambiar
REQUIRED=(MYSQL_ROOT_PASSWORD MYSQL_USER MYSQL_PASSWORD REDIS_PASSWORD
          ADMIN_PASSWORD ADMIN_KEY SECRET_KEY)
MISSING=()
while IFS='=' read -r key val; do
    [[ "$key" =~ ^[[:space:]]*# || -z "${key// /}" ]] && continue
    key="${key//[[:space:]]/}"; val="${val//[[:space:]]/}"
    if [[ " ${REQUIRED[*]} " == *" $key "* ]]; then
        if [[ -z "$val" || "$val" == *"cambia_esto"* || "$val" == *"tu_"* ]]; then
            MISSING+=("$key")
        fi
    fi
done < "$ENV_FILE"

[[ ${#MISSING[@]} -gt 0 ]] && die "Edita $CREDS_FILE — estas variables siguen con valores de ejemplo: ${MISSING[*]}"

# Retrofit: instalaciones existentes (credentials.conf ya generado antes de
# v1.16.0) no tienen KEYS_ENCRYPTION_SECRET — generarla acá si falta, una sola
# vez. --follow-symlinks: $CREDS_FILE puede ser un symlink al archivo real.
if ! grep -q "^KEYS_ENCRYPTION_SECRET=.\+" "$CREDS_FILE" 2>/dev/null; then
    _NEW_KEYS_SECRET=$(openssl rand 32 | openssl base64 -A | tr '+/' '-_')
    if grep -q "^KEYS_ENCRYPTION_SECRET=" "$CREDS_FILE"; then
        sed -i --follow-symlinks "s|^KEYS_ENCRYPTION_SECRET=.*|KEYS_ENCRYPTION_SECRET=${_NEW_KEYS_SECRET}|" "$CREDS_FILE"
    else
        echo "KEYS_ENCRYPTION_SECRET=${_NEW_KEYS_SECRET}" >> "$CREDS_FILE"
    fi
    ok "KEYS_ENCRYPTION_SECRET generada (instalación existente, v1.16.0)"
fi

# Retrofit: instalaciones existentes (credentials.conf ya generado antes de
# v1.19.0) no tienen INSTALL_SHERPA_LARGE — se pregunta acá mismo (mismo
# patrón que el resto de credentials.conf: primera vez pregunta, después ya
# queda guardado y no vuelve a preguntar). Con --yes/-y no pregunta, default
# false — no se activa un modelo de 1GB solo por automatizar el deploy.
if ! grep -q "^INSTALL_SHERPA_LARGE=" "$CREDS_FILE" 2>/dev/null; then
    echo ""
    ask_yn "¿Instalar también Sherpa Whisper Large (whisper-large-v3 completo, ~1GB, más preciso pero varios segundos por clip en CPU — nunca se usa como fallback automático)?" "false" INSTALL_SHERPA_LARGE
    echo "INSTALL_SHERPA_LARGE=$INSTALL_SHERPA_LARGE" >> "$CREDS_FILE"
    ok "INSTALL_SHERPA_LARGE=$INSTALL_SHERPA_LARGE guardada en credentials.conf (v1.19.0)"
fi

# El script no hace `source $CREDS_FILE` en ningún lado (las credenciales
# viajan a los contenedores vía `docker compose --env-file`, no como env del
# host) — así que sin esto, INSTALL_SHERPA_LARGE solo quedaba seteada en la
# sesión de bash donde se preguntó por primera vez. En la corrida siguiente
# (ej. un update normal) la variable volvía a estar vacía y el bloque de
# descarga de más abajo (`${INSTALL_SHERPA_LARGE:-false}`) nunca se disparaba
# aunque el archivo dijera true. Releer siempre desde el archivo — fuente
# única de verdad en cada corrida, se haya preguntado recién o no.
INSTALL_SHERPA_LARGE=$(grep -m1 '^INSTALL_SHERPA_LARGE=' "$CREDS_FILE" | cut -d= -f2-)

ok "Credenciales listas en $CREDS_FILE"

# ─── 2. Usuario de servicio (sin root) ───────────────────────────────────────
sep "Usuario de servicio"

if ! id "voxidet" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin voxidet
    ok "Usuario 'voxidet' creado"
else
    ok "Usuario 'voxidet' ya existe"
fi

# Docker puede ser ejecutado por el grupo docker
if command -v docker &>/dev/null; then
    usermod -aG docker voxidet 2>/dev/null || true
fi

# /opt/voxidet pertenece a voxidet (no a root)
mkdir -p "$DEPLOY_DIR"
chown -R voxidet:voxidet "$DEPLOY_DIR" 2>/dev/null || true

# ─── 3. Optimización del SO para carga concurrente ───────────────────────────
sep "Optimizando SO"

# File descriptors — cada WebSocket y conexión TCP consume 1 fd
if ! grep -q "nofile 65536" /etc/security/limits.conf 2>/dev/null; then
    cat >> /etc/security/limits.conf << 'EOF'
* soft nofile 65536
* hard nofile 65536
root soft nofile 65536
root hard nofile 65536
EOF
    ok "Límites de fd configurados (65536)"
else
    ok "Límites de fd ya configurados"
fi

# Parámetros TCP/red para muchas conexiones simultáneas
cat > /etc/sysctl.d/99-voxidet.conf << 'EOF'
# Backlog de conexiones entrantes
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
# Buffers de red más grandes
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
# Reutilizar puertos TIME_WAIT más rápido
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_tw_reuse = 1
# Más puertos efímeros disponibles
net.ipv4.ip_local_port_range = 1024 65535
# Límite de fd del sistema
fs.file-max = 262144
EOF
sysctl -p /etc/sysctl.d/99-voxidet.conf >/dev/null 2>&1
ok "Parámetros TCP/red aplicados"

# ─── 3. Dependencias del sistema ─────────────────────────────────────────────
sep "Dependencias"

if ! command -v docker &>/dev/null; then
    info "Instalando Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    ok "Docker instalado"
else
    ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"
fi

docker compose version &>/dev/null 2>&1 \
    || die "Docker Compose v2 no disponible. Actualiza Docker a versión >= 23."
ok "Docker Compose $(docker compose version --short)"

if ! command -v rsync &>/dev/null; then
    info "Instalando rsync..."
    apt-get install -y --no-install-recommends rsync >/dev/null 2>&1 \
        || yum install -y rsync >/dev/null 2>&1 \
        || die "Instala rsync manualmente y vuelve a correr deploy.sh"
fi

# ─── 3. Copiar proyecto a /opt/voxidet ─────────────────────────────────────────
sep "Copiando archivos a $DEPLOY_DIR"

mkdir -p "$DEPLOY_DIR"

rsync -a --delete \
    --exclude='.git/' \
    --exclude='credentials.conf' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='mysql_data/' \
    --exclude='redis_data/' \
    --exclude='models-local/' \
    "$SRC_DIR/" "$DEPLOY_DIR/"

# Symlink al mismo archivo real — no hay copia que sincronizar ni que pueda
# desincronizarse (antes existían dos .env: uno en $SRC_DIR y otro en
# $DEPLOY_DIR, con lógica para mantenerlos alineados; con un solo archivo
# real en $CREDS_FILE esa lógica ya no hace falta).
ln -sf "$CREDS_FILE" "$DEPLOY_DIR/credentials.conf"
ok "credentials.conf enlazado en $DEPLOY_DIR"

ok "Archivos en $DEPLOY_DIR"

# ─── 3b. Modelos locales ASR ─────────────────────────────────────────────────
sep "Modelos locales ASR"

MODELS_BASE="/opt/voxidet/models-local"
mkdir -p "$MODELS_BASE/vosk" "$MODELS_BASE/silero" "$MODELS_BASE/sherpa"

# ── Helper: descarga modelo si falta o si la versión cambió ──────────────────
# Uso: _ensure_model <ver_esperada> <ver_file> <check_path> <descarga_fn>
# check_path: archivo o directorio cuya existencia confirma que el modelo está
_ensure_model() {
    local expected_ver="$1"
    local ver_file="$2"
    local check_path="$3"
    local label="$4"
    shift 4
    # "$@" = función de descarga a llamar si hace falta

    local installed_ver=""
    [[ -f "$ver_file" ]] && installed_ver=$(cat "$ver_file")

    if [[ -e "$check_path" && "$installed_ver" == "$expected_ver" ]]; then
        ok "$label ($expected_ver) al día — sin descarga"
        return 0
    fi

    [[ -n "$installed_ver" && "$installed_ver" != "$expected_ver" ]] \
        && info "$label: $installed_ver → $expected_ver" \
        || info "Descargando $label ($expected_ver)..."

    "$@" && echo "$expected_ver" > "$ver_file" \
         || { echo -e "${YELLOW}[!] No se pudo descargar $label${NC}"; return 1; }
}

# ── Vosk ES ───────────────────────────────────────────────────────────────────
VOSK_MODEL="vosk-model-es-0.42"
VOSK_DIR="$MODELS_BASE/vosk/$VOSK_MODEL"
VOSK_VER_FILE="$MODELS_BASE/vosk/.version"
VOSK_URL="https://alphacephei.com/vosk/models/$VOSK_MODEL.zip"

_download_vosk() {
    wget -q --show-progress "$VOSK_URL" -O "/tmp/$VOSK_MODEL.zip" \
        && unzip -q "/tmp/$VOSK_MODEL.zip" -d "$MODELS_BASE/vosk/" \
        && rm -f "/tmp/$VOSK_MODEL.zip" \
        && ok "Vosk descargado en $VOSK_DIR"
}
_ensure_model "$VOSK_MODEL" "$VOSK_VER_FILE" "$VOSK_DIR" "Vosk ES" _download_vosk

# ── Silero VAD ────────────────────────────────────────────────────────────────
SILERO_FILE="$MODELS_BASE/silero/silero_vad.onnx"
SILERO_VER_FILE="$MODELS_BASE/silero/.version"
SILERO_URL="https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
SILERO_VERSION="v5.1"

_download_silero() {
    wget -q --show-progress "$SILERO_URL" -O "$SILERO_FILE" \
        && ok "Silero VAD descargado en $SILERO_FILE"
}
_ensure_model "$SILERO_VERSION" "$SILERO_VER_FILE" "$SILERO_FILE" "Silero VAD" _download_silero

# ── Sherpa-onnx (Whisper turbo cuantizado int8, ~538MB) ──────────────────────
# Antes NUNCA se descargaba acá — el proveedor "Sherpa-onnx (local)" del panel
# quedaba permanentemente "no cargado" sin ninguna forma de activarlo, aunque
# el código de carga (core/local_asr.py) estuviera listo desde antes. Mismo
# modelo que ya asume _DEFAULTS["sherpa"] en db/providers.py (whisper-large-v3-
# turbo) — no el large-v3 completo (1GB, demasiado lento en CPU para AMD en
# tiempo real, ver CHANGELOG).
SHERPA_MODEL="sherpa-onnx-whisper-turbo"
SHERPA_DIR="$MODELS_BASE/sherpa/$SHERPA_MODEL"
SHERPA_VER_FILE="$MODELS_BASE/sherpa/.version"
SHERPA_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-turbo.tar.bz2"

_download_sherpa() {
    wget -q --show-progress "$SHERPA_URL" -O "/tmp/$SHERPA_MODEL.tar.bz2" \
        && tar -xjf "/tmp/$SHERPA_MODEL.tar.bz2" -C "$MODELS_BASE/sherpa/" \
        && rm -f "/tmp/$SHERPA_MODEL.tar.bz2" \
        && ok "Sherpa (Whisper turbo) descargado en $SHERPA_DIR"
}
_ensure_model "$SHERPA_MODEL" "$SHERPA_VER_FILE" "$SHERPA_DIR" "Sherpa Whisper turbo" _download_sherpa

# ── Sherpa-onnx Whisper large-v3 completo (opt-in, ~1GB, v1.19.0) ────────────
# A diferencia de todo lo demás en esta sección, NO se descarga por defecto:
# es ~2x el tamaño de turbo y varios segundos por clip en CPU (decoder de 32
# capas vs 4 de turbo) — mala idea como default para todas las instalaciones
# VoxiDet, buena idea solo para quien lo pida a propósito. Activar con
# INSTALL_SHERPA_LARGE=true en credentials.conf. Además queda con active=0 en
# provider_settings por default (ver db/providers.py) y nunca se usa como
# fallback automático (ver _NEVER_AUTO_FALLBACK en amd_engine.py) — hay que
# elegirlo a propósito en dos lugares distintos, no por accidente en ninguno.
mkdir -p "$MODELS_BASE/sherpa_large"
if [[ "${INSTALL_SHERPA_LARGE:-false}" == "true" ]]; then
    SHERPA_LARGE_MODEL="sherpa-onnx-whisper-large-v3"
    SHERPA_LARGE_DIR="$MODELS_BASE/sherpa_large/$SHERPA_LARGE_MODEL"
    SHERPA_LARGE_VER_FILE="$MODELS_BASE/sherpa_large/.version"
    SHERPA_LARGE_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-large-v3.tar.bz2"

    _download_sherpa_large() {
        wget -q --show-progress "$SHERPA_LARGE_URL" -O "/tmp/$SHERPA_LARGE_MODEL.tar.bz2" \
            && tar -xjf "/tmp/$SHERPA_LARGE_MODEL.tar.bz2" -C "$MODELS_BASE/sherpa_large/" \
            && rm -f "/tmp/$SHERPA_LARGE_MODEL.tar.bz2" \
            && ok "Sherpa (Whisper large-v3 completo) descargado en $SHERPA_LARGE_DIR"
    }
    _ensure_model "$SHERPA_LARGE_MODEL" "$SHERPA_LARGE_VER_FILE" "$SHERPA_LARGE_DIR" "Sherpa Whisper large-v3" _download_sherpa_large
else
    info "Sherpa Whisper large-v3 completo: desactivado (INSTALL_SHERPA_LARGE=true en credentials.conf para activarlo — ~1GB, varios segundos por clip en CPU, no recomendado como fallback automático)"
fi

# ─── 3c. Autotuneo de recursos (workers, memoria, open files) ────────────────
# El objetivo: nunca pedir más CPU/RAM de la que el host realmente tiene, y
# escalar automáticamente cuando se migra a un servidor más grande o más
# chico — sin editar nada a mano. Fórmula centralizada en scripts/autotune.sh
# (mismo script que corre solo en cada arranque vía voxidet-autotune.service,
# para cuando el host cambia de CPU/RAM sin que se corra deploy.sh a mano).
sep "Autotuneo de recursos"
chmod +x "$DEPLOY_DIR/scripts/autotune.sh"
"$DEPLOY_DIR/scripts/autotune.sh" || echo -e "${YELLOW}[!] autotune.sh terminó con errores — revisar arriba${NC}"
# docker compose up -d --force-recreate (más abajo) ya recrea los contenedores
# con los valores recién escritos — no hace falta pedirle a autotune.sh que
# lo haga también en esta corrida.

# ─── 4. Firewall (nftables) ──────────────────────────────────────────────────
sep "Firewall"

if ! command -v nft &>/dev/null; then
    info "Instalando nftables..."
    apt-get install -y --no-install-recommends nftables >/dev/null 2>&1 \
        || yum install -y nftables >/dev/null 2>&1 \
        || { echo -e "${YELLOW}[!] No se pudo instalar nftables — continúa sin firewall${NC}"; }
fi

if command -v nft &>/dev/null; then
    # Crear directorio para reglas parciales
    mkdir -p /etc/nftables.d

    # Detección de puerto SSH — sshd_config primero (autoritativo, mismo orden
    # que detect_ssh_ports() en gen_nftables.py). "ss | grep sshd" como único
    # respaldo puede matchear un socket de X11 forwarding de una sesión SSH
    # activa (ej. puerto 6010 = 6000+display 10 de `ssh -X`), no el puerto
    # real de sshd — por eso NO va primero.
    SSH_PORT=$(grep -E '^\s*Port\s+[0-9]' /etc/ssh/sshd_config 2>/dev/null | awk '{print $2}' | head -1 || true)
    [[ -z "$SSH_PORT" ]] && SSH_PORT=$(ss -tlnp 2>/dev/null | grep sshd | awk '{print $4}' | grep -oP '\d+$' | head -1 || true)
    [[ -z "$SSH_PORT" ]] && SSH_PORT=22
    ok "Puerto SSH detectado: $SSH_PORT"

    # Nombre del unit systemd de SSH — ssh.service (Debian/Ubuntu) vs
    # sshd.service (RHEL/CentOS/Fedora). Se busca entre las unit *files*
    # (no las activas) para que funcione aunque el servicio esté detenido.
    SSH_SERVICE=$(systemctl list-unit-files --type=service 2>/dev/null \
        | awk '{print $1}' | grep -E '^sshd?\.service$' | head -1 || true)
    [[ -z "$SSH_SERVICE" ]] && SSH_SERVICE="ssh.service"
    ok "Servicio SSH detectado: $SSH_SERVICE"

    # WEB_PORT — API_PORT en credentials.conf si se personalizó, si no el default 8000
    # (mismo override que ya soporta gen_nftables.py).
    WEB_PORT=$(grep -m1 '^API_PORT=' "$CREDS_FILE" 2>/dev/null | cut -d= -f2- || true)
    WEB_PORT="${WEB_PORT:-8000}"

    # apply_conf — mismo patrón que VoxiKam: sed sobre el archivo fuente del
    # repo, nunca se edita el destino en /etc a mano.
    apply_conf() {
        local src="$1" dst="$2"
        sed \
            -e "s|__SSH_PORT__|${SSH_PORT}|g" \
            -e "s|__SSH_SERVICE__|${SSH_SERVICE}|g" \
            -e "s|__WEB_PORT__|${WEB_PORT}|g" \
            "$src" > "$dst"
    }

    # Config base — copiada desde el repo (nftables/), no generada inline.
    apply_conf "$SRC_DIR/nftables/nftables.conf" /etc/nftables.conf
    ok "/etc/nftables.conf (SSH_PORT=$SSH_PORT, WEB_PORT=$WEB_PORT)"

    # Placeholder — evita que nftables.service falle con "File not found" si
    # gen_nftables.py aún no pudo escribir el fragmento real (p.ej. DB todavía
    # no está arriba porque Docker Compose corre después de este bloque).
    [[ -f /etc/nftables.d/voxidet.nft ]] || cp "$SRC_DIR/nftables/nftables.d/voxidet.nft" /etc/nftables.d/voxidet.nft

    # Sudoers — copiado desde el repo (sudoers/), no generado inline
    cp "$SRC_DIR/sudoers/voxidet" /etc/sudoers.d/voxidet
    chmod 440 /etc/sudoers.d/voxidet
    visudo -c -f /etc/sudoers.d/voxidet >/dev/null || die "sudoers/voxidet tiene sintaxis inválida"

    # Marcador de instalación para que gen_nftables.py encuentre credentials.conf
    echo "INSTALL_DIR=$DEPLOY_DIR" > /etc/voxidet.conf

    # Cron safety net: re-aplica reglas cada 5 minutos
    CRON_FILE="/etc/cron.d/voxidet-firewall"
    cat > "$CRON_FILE" << EOF
# VoxiDet firewall — re-aplica nftables cada 5 min (safety net)
*/5 * * * * voxidet /usr/bin/python3 $DEPLOY_DIR/scripts/gen_nftables.py >> /var/log/voxidet-fw.log 2>&1
EOF

    # Aplicar reglas iniciales (sin bloquear nada — tabla vacía)
    python3 "$DEPLOY_DIR/scripts/gen_nftables.py" 2>/dev/null && ok "nftables aplicado" \
        || info "nftables: tabla vacía (sin reglas configuradas aún)"

    systemctl enable --now nftables 2>/dev/null || true
    ok "Firewall listo — gestionar desde el panel admin en /firewall"

    # nft -f ejecuta "flush ruleset", que borra TODO el ruleset del sistema —
    # incluyendo las cadenas iptables-nft que Docker ya había programado
    # (DOCKER, NAT, etc). Sin este restart, docker compose falla al mapear
    # puertos con "iptables: No chain/target/match by that name".
    info "Reiniciando Docker para reprogramar sus cadenas nftables..."
    systemctl restart docker
    sleep 3
else
    echo -e "${YELLOW}[!] nftables no disponible — servidor sin firewall de red${NC}"
fi

# ─── 4b. fail2ban (SSH + rechazos de seguridad de la app) ────────────────────
sep "fail2ban"

# Directorio de logs — bind mount (no volumen Docker nombrado) para que
# fail2ban, que corre en el host, pueda leer security.log directo.
mkdir -p /opt/voxidet/logs

# fail2ban (backend=auto) falla al arrancar si el logpath está en 0 bytes —
# "Have not found any log file for X jail" — antes de que la app escriba su
# primer rechazo (caso normal en un deploy nuevo, sin ataques todavía) el
# archivo existe pero vacío. Se asegura al menos una línea antes de (re)iniciar.
touch /opt/voxidet/logs/security.log
[[ -s /opt/voxidet/logs/security.log ]] || echo "# inicializado por deploy.sh" >> /opt/voxidet/logs/security.log

# chown DESPUÉS de crear security.log (no antes): el touch de arriba corre
# como root, así que el archivo nace root:root — si el chown del directorio
# fuera antes, security.log quedaría sin permiso de escritura para uid 1000
# (usuario "amd" dentro del contenedor), y el RotatingFileHandler de
# log_config.json ("security_file") fallaría con PermissionError al abrirlo
# ("ValueError: Unable to configure handler 'security_file'"). Recursivo por
# si en el futuro se agregan más archivos pre-creados aquí.
chown -R 1000:1000 /opt/voxidet/logs

info "Instalando/actualizando dependencias de fail2ban..."
# Incondicional (no solo "si falta fail2ban-client"): apt-get install es
# idempotente, y así una dependencia nueva (ej. python3-systemd, agregado
# después de que fail2ban ya estaba instalado en servidores previos) se
# garantiza en cada deploy, no solo en la instalación inicial.
# python3-systemd: bindings que fail2ban necesita para backend=systemd
# (jail sshd lee journald). Sin este paquete el jail sshd falla al
# inicializar ("No module named 'systemd'") y arrastra a TODO el
# servicio fail2ban (exit 255, no reinicia por RestartPreventExitStatus).
apt-get install -y --no-install-recommends fail2ban python3-systemd >/dev/null 2>&1 \
    || yum install -y fail2ban python3-systemd >/dev/null 2>&1 \
    || echo -e "${YELLOW}[!] No se pudo instalar fail2ban — continúa sin esta capa${NC}"

if command -v fail2ban-client &>/dev/null; then
    mkdir -p /etc/fail2ban/filter.d /etc/fail2ban/jail.d
    cp "$SRC_DIR/fail2ban/filter.d/voxidet-security.conf" /etc/fail2ban/filter.d/voxidet-security.conf
    sed -e "s|__SSH_PORT__|${SSH_PORT}|g" -e "s|__SSH_SERVICE__|${SSH_SERVICE}|g" \
        "$SRC_DIR/fail2ban/jail.d/voxidet.conf" > /etc/fail2ban/jail.d/voxidet.conf
    ok "jails copiados (sshd puerto $SSH_PORT, voxidet-security)"

    systemctl enable fail2ban 2>/dev/null
    systemctl restart fail2ban \
        && ok "fail2ban activo" \
        || echo -e "${YELLOW}[!] fail2ban no arrancó — revisar: journalctl -u fail2ban -n 30${NC}"

    # Bridge host↔contenedor: procesa unbans encolados por el panel y publica
    # el estado (IPs baneadas) a un JSON que el panel lee vía el bind mount
    # de logs — el contenedor no puede llamar fail2ban-client directo.
    CRON_F2B="/etc/cron.d/voxidet-fail2ban"
    cat > "$CRON_F2B" << EOF
# VoxiDet — procesa unbans y publica estado de fail2ban cada minuto
* * * * * root /usr/bin/python3 $DEPLOY_DIR/scripts/fail2ban_bridge.py >> /var/log/voxidet-fail2ban.log 2>&1
EOF
    python3 "$DEPLOY_DIR/scripts/fail2ban_bridge.py" 2>/dev/null && ok "fail2ban-status.json inicial escrito" \
        || info "fail2ban_bridge.py: primera corrida falló (normal si fail2ban aún no procesó ningún jail) — el cron reintenta cada minuto"
else
    echo -e "${YELLOW}[!] fail2ban no disponible — sin protección de fuerza bruta${NC}"
fi

# ─── 5. Levantar con Docker Compose ──────────────────────────────────────────
sep "Levantando contenedores"

cd "$DEPLOY_DIR/docker"
COMPOSE="docker compose --env-file $CREDS_FILE"

info "Descargando imágenes base (mysql, redis)..."
$COMPOSE pull --quiet mysql redis 2>/dev/null || true

info "Construyendo imagen API..."
$COMPOSE build --quiet api

info "Iniciando servicios (force-recreate)..."
$COMPOSE up -d --force-recreate

# Docker tiene restart:always — los contenedores arrancan solos si el VPS se
# reinicia, pero con los valores de recursos que ya había en credentials.conf (no
# recalcula nada). voxidet-autotune.service sí recalcula en cada arranque —
# útil si el host cambió de CPU/RAM (resize + reboot en el proveedor cloud)
# sin que alguien corra deploy.sh a mano. VoxiDet no mantiene diálogos SIP en
# vivo (a diferencia de Kamailio en VoxiKam) — el dialplan ya tiene fallback
# a ERROR si una detección falla a medio camino, así que recrear contenedores
# automáticamente en el arranque es de bajo riesgo.
sep "Autotuneo automático en cada arranque"
cat > /etc/systemd/system/voxidet-autotune.service << EOF
[Unit]
Description=VoxiDet — autotuneo de recursos al arrancar (CPU/RAM reales)
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
ExecStart=$DEPLOY_DIR/scripts/autotune.sh --apply
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal
SyslogIdentifier=voxidet-autotune

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable voxidet-autotune >/dev/null 2>&1
ok "voxidet-autotune habilitado para el próximo arranque (no corre ahora — el tuning de esta corrida ya lo hizo deploy.sh)"

# ─── 5. Verificar que levantó correctamente ───────────────────────────────────
sep "Healthcheck"

# Chequeo por contenedor (mismo patrón que chk_svc() de VoxiKam para sus
# servicios systemd, adaptado a Docker) — antes solo se validaba el agregado
# /health, que no distingue "el contenedor no llegó a levantar" de "levantó
# pero tarda en responder".
chk_container() {
    docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true \
        && ok "$1 corriendo" \
        || { warn "$1 no está corriendo — revisar: docker logs $1"; ALL_OK=false; }
}
chk_container voxidet-mysql
chk_container voxidet-redis
chk_container voxidet-api

info "Esperando API (máx 60s)..."
COUNT=0
until curl -sf http://127.0.0.1:8000/health 2>/dev/null | grep -q '"status":"ok"'; do
    COUNT=$((COUNT + 1))
    if [[ $COUNT -ge 30 ]]; then
        echo ""
        warn "La API no respondió a tiempo — revisar con: docker compose --env-file $CREDS_FILE -f $DEPLOY_DIR/docker/docker-compose.yml logs api"
        echo "  Últimas líneas:"
        $COMPOSE logs --tail=20 api
        ALL_OK=false
        break
    fi
    printf "."
    sleep 2
done
echo ""

[[ "$ALL_OK" == true ]] && ok "$(curl -s http://127.0.0.1:8000/health)"

# ─── Restaurar datos migrados de la instalación anterior (si aplica) ─────────
if [[ "$MIGRATE_PENDING" -eq 1 && "$ALL_OK" == false ]]; then
    warn "Healthcheck falló — se omite la restauración de datos migrados (no tiene sentido escribir contra una API/DB que no respondió). El dump sigue disponible en $MIGRATE_DUMP — restaurar a mano una vez resuelto el problema."
elif [[ "$MIGRATE_PENDING" -eq 1 ]]; then
    sep "Restaurando datos migrados"
    NEW_ROOT_PW=$(grep -m1 '^MYSQL_ROOT_PASSWORD=' "$CREDS_FILE" | cut -d= -f2- || true)

    # voxidet_keywords y provider_settings se auto-siembran (INSERT IGNORE,
    # IDs/keys propios) apenas arranca la API — truncar TODAS las tablas de
    # datos antes de restaurar evita choques parciales (si el dump se corta
    # a mitad de camino por una sola tabla, el resto queda en un estado
    # mixto difícil de diagnosticar). FK_CHECKS=0 porque clients tiene hijos.
    info "Limpiando tablas antes de restaurar (evita choques con datos semilla)..."
    docker exec -i voxidet-mysql mysql -u root -p"$NEW_ROOT_PW" voxidet_db <<'SQL' 2>/dev/null || true
SET FOREIGN_KEY_CHECKS=0;
TRUNCATE TABLE voxidet_keywords;
TRUNCATE TABLE clients;
TRUNCATE TABLE client_keywords;
TRUNCATE TABLE daily_usage;
TRUNCATE TABLE firewall_rules;
TRUNCATE TABLE provider_settings;
TRUNCATE TABLE provider_stats;
TRUNCATE TABLE voxidet_logs;
SET FOREIGN_KEY_CHECKS=1;
SQL

    docker exec -i voxidet-mysql mysql -u root -p"$NEW_ROOT_PW" voxidet_db < "$MIGRATE_DUMP"
    ok "Datos restaurados en voxidet_db desde $MIGRATE_DUMP"
    echo ""
    echo -e "  ${YELLOW}El stack anterior sigue en /opt/vdamd, sin tocar, como red de seguridad.${NC}"
    echo "  Cuando confirmes que todo funciona, limpia a mano:"
    echo "    userdel vdamd; rm -rf /opt/vdamd /etc/sudoers.d/vdamd /etc/cron.d/vdamd-firewall /etc/vdamd.conf /etc/nftables.d/vdamd.nft"
    echo "    sed -i '/vdamd.nft/d' /etc/nftables.conf"
    echo "    docker volume ls | grep vdamd   # confirma el nombre exacto antes de borrar"
fi

# ─── Marcador del sistema — mismo patrón que VoxiKam (/etc/voxikam.conf) ─────
# Se escribe (o actualiza VERSION si ya existía) sin importar el resultado del
# healthcheck — el código quedó desplegado igual, aunque la API haya tardado
# en responder; un healthcheck lento no significa que la versión no se aplicó.
if [[ -f "$MARKER_FILE" ]]; then
    # sed con `s/^VERSION=.../` no hace nada (0 reemplazos, exit 0 igual) si el
    # archivo no tiene ninguna línea VERSION= — pasaba con marcadores viejos de
    # antes de que este campo existiera, y quedaba reportando "vdesconocida"
    # para siempre aunque el deploy sí haya actualizado el código. Si no existe
    # la línea, se agrega en vez de asumir que sed ya la creó.
    if grep -q "^VERSION=" "$MARKER_FILE"; then
        sed -i "s/^VERSION=.*/VERSION=${PLATFORM_VERSION}/" "$MARKER_FILE"
    else
        echo "VERSION=${PLATFORM_VERSION}" >> "$MARKER_FILE"
    fi
    ok "Marcador actualizado → v${PLATFORM_VERSION}"
else
    cat > "$MARKER_FILE" <<EOF
# VoxiDet — archivo de configuración del sistema
# Generado por deploy.sh — no editar manualmente
INSTALL_DIR=$DEPLOY_DIR
CREDS_FILE=$CREDS_FILE
INSTALL_DATE=$(date -Iseconds)
VERSION=$PLATFORM_VERSION
EOF
    chmod 644 "$MARKER_FILE"
    ok "Marcador del sistema → $MARKER_FILE (v${PLATFORM_VERSION})"
fi

# ─── Resumen final ────────────────────────────────────────────────────────────
_ELAPSED=$(( SECONDS - INSTALL_START ))
_ELAPSED_FMT="$(( _ELAPSED / 60 ))m $(( _ELAPSED % 60 ))s"
_BAR="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo ""
if [[ "$ALL_OK" == false ]]; then
    echo -e "${YELLOW}${_BAR}${NC}"
    echo -e "${YELLOW}  Instalación con errores — revisar ✗${NC}"
    echo -e "${YELLOW}${_BAR}${NC}"
    echo ""
    echo "  Directorio: $DEPLOY_DIR"
    echo "  Creds:      $CREDS_FILE"
    echo "  Log:        ${LOG_FILE:-(sin log en disco)}"
    echo ""
    echo "  Diagnóstico:"
    echo "    docker compose --env-file $CREDS_FILE -f $DEPLOY_DIR/docker/docker-compose.yml logs api"
    echo "    docker compose --env-file $CREDS_FILE -f $DEPLOY_DIR/docker/docker-compose.yml ps"
    echo ""
    echo "  Tiempo: $_ELAPSED_FMT"
else
    echo -e "${GREEN}${_BAR}${NC}"
    echo -e "${GREEN}  ${PLATFORM_NAME:-VoxiDet} v${PLATFORM_VERSION} corriendo en $DEPLOY_DIR ✓${NC}"
    echo -e "${GREEN}${_BAR}${NC}"
    echo ""
    echo "  Tiempo: $_ELAPSED_FMT"
    echo ""
    echo "  Gestión del servicio (desde cualquier lugar):"
    echo "    Iniciar/detener:  docker compose --env-file $CREDS_FILE -f $DEPLOY_DIR/docker/docker-compose.yml up -d / down"
    echo "    Ver logs:         docker compose --env-file $CREDS_FILE -f $DEPLOY_DIR/docker/docker-compose.yml logs -f api"
    echo "    Estado:           docker compose --env-file $CREDS_FILE -f $DEPLOY_DIR/docker/docker-compose.yml ps"
    echo ""
    echo "  Siguiente paso — crear primer cliente:"
    echo "    docker exec -it voxidet-api python manage.py add-client \"vd1atk1\" --limit 100000"
    echo ""
    echo "  Eso imprime el INSTALL_TOKEN. Luego en cada nodo Asterisk:"
    echo "    wget <PUBLIC_URL>/install/<INSTALL_TOKEN> -O /var/lib/asterisk/agi-bin/amd_ia.agi"
    echo "    chmod 755 /var/lib/asterisk/agi-bin/amd_ia.agi"
    echo ""
    echo "  Actualizar en el futuro:"
    echo "    cd $SRC_DIR && git pull && sudo bash deploy.sh"
fi
echo ""
echo "  Visítanos en: github.com/KPBTec"
echo "  ${PLATFORM_NAME:-VoxiDet} · un desarrollo de KPBTec"
echo ""
