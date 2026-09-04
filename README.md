<div align="center">

<img src="docs/logo.svg" alt="VoxiDet" width="300"/>

### Detección AMD con IA para contact centers Asterisk/Vicidial

[![Version](https://img.shields.io/badge/version-1.28.0-e8a262?style=flat-square)](CHANGELOG.md)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Ubuntu%2022.04%20%2F%20Debian%2012-orange?style=flat-square)](#requisitos-del-vps)
[![Telegram](https://img.shields.io/badge/soporte-Telegram-2CA5E0?style=flat-square&logo=telegram)](https://t.me/sktcod)

*Reemplaza el AMD nativo de Asterisk (impreciso) por un servidor externo que analiza*
*el audio de cada llamada y devuelve HUMAN, VOICEMAIL o UNKNOWN.*

**Un producto de [KPBTec](https://github.com/KPBTec) · Knowledge, Protection & Business Technology**

</div>

---

- **Capa 1** — análisis de energía de audio, gratis, <100ms
- **Capa 2** — transcripción con IA (Groq / Deepgram / OpenAI / Together / Fireworks / Vosk / Sherpa-onnx, con fallback automático entre proveedores) como respaldo, ~300ms

---

## Requisitos del VPS

- Ubuntu 22.04 / Debian 12 (recomendado)
- 1 vCPU, 1 GB RAM mínimo (2 vCPU / 2 GB recomendado)
- Acceso root o sudo

No necesitas instalar Python, pip ni ninguna librería. Todo corre dentro de Docker.

---

## 1. Instalar Docker

> **No necesitas instalar Python.** Todo corre dentro de los contenedores.

```bash
# Dependencias previas
apt update
apt install -y ca-certificates curl gnupg

# Agregar repositorio oficial de Docker
# Funciona para Ubuntu y Debian automáticamente
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg \
     -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt update

# Instalar Docker + Compose plugin
apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Verificar
docker --version
docker compose version

# Arrancar con el sistema
systemctl enable docker
systemctl start docker
```

---

## 2. Instalar Cloudflare Tunnel (opcional pero recomendado)

Si tienes un dominio en Cloudflare, el tunnel te da HTTPS gratis sin abrir puertos.

```bash
# Descargar cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
     -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# Autenticar con tu cuenta Cloudflare
cloudflared tunnel login

# Crear el tunnel
cloudflared tunnel create voxidet

# Configurar en Cloudflare Zero Trust Dashboard:
#   Tunnel → voxidet → Public Hostname
#   Domain: amd.tudominio.com
#   Service: http://localhost:8000
```

---

## 3. Clonar y configurar

```bash
git clone <tu-repo> voxidet
cd voxidet
sudo bash deploy.sh
```

En la primera corrida, si no existen credenciales todavía, `deploy.sh` detecta la IP pública solo, pregunta lo esencial (URL pública, usuario/password admin) y genera el resto de los secretos — no hace falta crear ni editar ningún archivo a mano. El archivo real queda en `/voxidet-install/logs-configs/credentials.conf`, con un symlink `credentials.conf` en la raíz del proyecto para editarlo fácil si hace falta después (agregar API keys de transcripción, etc).

### Variables de `credentials.conf`

```ini
# Base de datos
MYSQL_ROOT_PASSWORD=password_seguro
MYSQL_USER=amd_user
MYSQL_PASSWORD=password_seguro

# Redis
REDIS_PASSWORD=password_seguro

# Deepgram (obtener en https://deepgram.com)
DEEPGRAM_API_KEY=tu_api_key

# URL pública (con Cloudflare Tunnel: tu dominio HTTPS)
PUBLIC_URL=https://amd.tudominio.com

# Panel admin — cambiar el path, nunca dejar /admin
ADMIN_PREFIX=/tupath secreto
ADMIN_USER=admin
ADMIN_PASSWORD=password_seguro

# Clave para firmar sesiones (generar con el comando de abajo)
SECRET_KEY=

# Clave para acceso JSON programático
ADMIN_KEY=
```

Generar claves seguras:
```bash
# SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"

# ADMIN_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## 4. Levantar el servidor

`docker-compose.yml` vive en `docker/` — `credentials.conf` es un symlink en la raíz del proyecto que apunta al archivo real en `/voxidet-install/logs-configs/`.

```bash
cd docker
docker compose up -d --build

# Verificar que todo está corriendo
docker compose ps

# Health check
curl http://localhost:8000/health
# → {"status":"ok","db":"ok","cache":"ok"}
```

---

## 5. Crear el primer cliente

```bash
docker exec -it voxidet-api python cli/manage.py add-client "Nombre del cliente" --limit 500000

# Salida:
# ✅ Cliente creado
#    API Key       : xK9mP2...  (interno, no compartir)
#    Install Token : rT7nQ4...  (para descargar el AGI)
#    📦 wget https://amd.tudominio.com/install/rT7nQ4... -O /var/lib/asterisk/agi-bin/amd_ia.agi
```

O desde el panel web en `https://amd.tudominio.com/tupath/login`.

---

## 6. Panel de administración

Acceder en: `https://amd.tudominio.com/ADMIN_PREFIX/login`

| Sección | Funcionalidad |
|---|---|
| **Clientes** | Crear, activar/desactivar, editar IPs, copiar URL de instalación, rotar tokens |
| **Logs en vivo** | Stream en tiempo real, filtros por teléfono / resultado / UniqueID |

---

## 7. Configurar en Asterisk (cada nodo ATK)

El cliente recibe su URL de instalación y ejecuta:

```bash
# Descargar el AGI pre-configurado (ya tiene servidor y token incrustados)
wget https://amd.tudominio.com/install/INSTALL_TOKEN \
     -O /var/lib/asterisk/agi-bin/amd_ia.agi
chmod 755 /var/lib/asterisk/agi-bin/amd_ia.agi
```

En el dialplan, reemplazar el `AMD()` nativo por:

```ini
same => n,AGI(amd_ia.agi)
same => n,GotoIf($["${AMDSTATUS}" = "HUMAN"]?transfer_agent,1)
same => n,GotoIf($["${AMDSTATUS}" = "VOICEMAIL"]?voicemail,1)
same => n,Hangup()
```

Variables de canal que setea el AGI:
- `AMDSTATUS` → `HUMAN` | `VOICEMAIL` | `UNKNOWN` | `ERROR`
- `AMDLAYER`  → `1` (energía) o `2` (transcripción por IA)
- `AMDMS`     → latencia en ms

### Modos de detección (por cliente, desde el panel → Clientes → Editar)

| Modo | Cómo funciona | Dialplan |
|---|---|---|
| **Batch** (default) | El AGI graba un fragmento corto y lo envía de una — la opción más simple, funciona en cualquier Asterisk sin tocar nada más. | El de arriba, sin cambios. |
| **Stream** | Analiza el audio en vivo mientras la llamada progresa (EAGI), decide más rápido en más casos. Es unidireccional (no puede reproducir audio de vuelta mientras analiza). | Mismo `AGI(amd_ia.agi)`, el AGI decide el modo solo consultando la config del cliente en el servidor. |
| **Audiosocket** | Transporte bidireccional nativo de Asterisk (`AudioSocket()`) — soluciona el problema de llamadas que quedaban en silencio muerto durante el análisis, mandando audio de confort de vuelta mientras decide. Requiere 3 líneas extra en el dialplan de ese nodo puntual (no es automático). | Ver el dialplan de referencia completo, ya armado con el dominio y puerto reales de tu servidor, en el panel: **Clientes → Editar cliente → modo Audiosocket**. Verificar antes que el nodo tenga los módulos `app_audiosocket`, `chan_audiosocket` y `res_audiosocket` cargados (`asterisk -rx "module show like audiosocket"`). |

El modo se elige por cliente, no es global — se puede tener clientes en Batch y otros en Stream/Audiosocket al mismo tiempo en el mismo servidor VoxiDet.

> **Nota sobre Audiosocket y Asterisk 18.26.4:** en esa versión puntual, la aplicación de dialplan
> `AudioSocket()` nunca continúa a la siguiente línea del dialplan pase lo que pase (confirmado contra el
> código fuente de esa versión) — un cliente HUMAN detectado bien igual termina colgado en vez de
> transferido al agente. Para ese caso existe el **modo ARI (experimental)**, ver más abajo.

---

## Modo ARI (experimental, v1.28.0)

Soluciona la limitación de arriba: en vez de que el dialplan dependa de que `AudioSocket()` "no falle",
un servicio aparte (`ari-controller`, otro contenedor, no otro worker de la API) controla la llamada
directamente vía la API REST/WebSocket de Asterisk (ARI) y le dice explícitamente cuándo continuar.

**Usar solo en una extensión de prueba nueva (`8379` por convención) — nunca en la extensión real de un
cliente en producción**, hasta validarlo en vivo.

### 1. Habilitar ARI en Asterisk

```ini
# /etc/asterisk/ari.conf
[general]
enabled = yes

[voxidet]
type = user
password = una_contraseña_fuerte
; NO agregar read_only = yes — el controlador necesita crear canales/bridges
```

```bash
asterisk -rx "module reload res_ari.so"
```

`http.conf` debe tener `enabled=yes` (viene así en la mayoría de las instalaciones — confirmar con
`asterisk -rx "http show status"`).

### 2. Configurar `credentials.conf`

```ini
ARI_URL=http://IP_DEL_ASTERISK:8088
ARI_USER=voxidet
ARI_PASSWORD=la_misma_que_pusiste_en_ari.conf
ARI_APP=voxidet-ari
# "rtp" si el Asterisk NO tiene chan_audiosocket instalado (caso más común),
# "audiosocket" si sí lo tiene (ver sección Audiosocket de arriba)
ARI_MEDIA_MODE=rtp
# IP de este servidor VoxiDet, alcanzable desde el Asterisk por la red interna
AUDIOSOCKET_HOST=10.0.0.x
```

`sudo bash deploy.sh` levanta el contenedor `ari-controller` — si `ARI_URL`/`ARI_PASSWORD` quedan vacíos,
el contenedor arranca pero no hace nada (lo avisa en sus logs).

### 3. Dialplan de la extensión de prueba

```
exten => 8379,1,AGI(agi://127.0.0.1:4577/call_log)
exten => 8379,n,Playback(sip-silence)
exten => 8379,n,Wait(0.5)
exten => 8379,n,Set(VOXIDET_API_KEY=api_key_del_cliente_de_prueba)
exten => 8379,n,Stasis(voxidet-ari)
exten => 8379,n(after-ari),NoOp(AMD: ${AMDSTATUS} capa=${AMDLAYER} ${AMDMS}ms)
exten => 8379,n,GotoIf($["${AMDSTATUS}"="HUMAN"]?human)
exten => 8379,n,Hangup()
exten => 8379,n(human),AGI(agi-VDAD_ALL_outbound.agi,NORMAL-----LB-----${CONNECTEDLINE(name)})
exten => 8379,n,Hangup()
```

Es el mismo patrón que Batch/Stream (`NoOp`/`GotoIf`/rama `human` sin cambios) — solo la línea de
`Stasis(voxidet-ari)` en vez de `EAGI(amd_ia.agi)`/`AudioSocket(...)`.

**Nunca probado contra Asterisk real todavía** — ver `CLAUDE.md` § "5. Modo ARI" para el detalle exacto
de qué se validó (test sintético, no en vivo) y qué falta confirmar antes de usarlo con tráfico real.

---

## Operaciones del día a día

Corre estos comandos desde `docker/` (o usa `-f docker/docker-compose.yml --env-file credentials.conf` desde la raíz):

```bash
# Ver logs en tiempo real
docker compose logs -f api

# Reiniciar sin perder datos
docker compose restart api

# Actualizar después de cambios en el código
docker compose up -d --build api

# Gestión de clientes (CLI)
docker exec -it voxidet-api python cli/manage.py list-clients
docker exec -it voxidet-api python cli/manage.py set-ips
docker exec -it voxidet-api python cli/manage.py stats
```

---

## Costos estimados

| Componente | Costo mensual |
|---|---|
| VPS (2 vCPU, 2 GB RAM) | ~$6–10 |
| Deepgram (~30% de llamadas a capa 2) | ~$20–30 |
| **Total para 10k llamadas/día** | **~$26–40** |

---

## Licencia

Este proyecto está licenciado bajo los términos de la [Licencia AGPL v3](LICENSE), que requiere que cualquier modificación distribuida o usada como servicio de red sea publicada bajo los mismos términos.
