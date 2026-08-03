<div align="center">

<img src="docs/logo.svg" alt="VoxiDet" width="300"/>

### Detección AMD con IA para contact centers Asterisk/Vicidial

[![Version](https://img.shields.io/badge/version-1.13.0-e8a262?style=flat-square)](CHANGELOG.md)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Ubuntu%2022.04%20%2F%20Debian%2012-orange?style=flat-square)](#requisitos-del-vps)
[![Telegram](https://img.shields.io/badge/soporte-Telegram-2CA5E0?style=flat-square&logo=telegram)](https://t.me/sktcod)

*Reemplaza el AMD nativo de Asterisk (impreciso) por un servidor externo que analiza*
*el audio de cada llamada y devuelve HUMAN, VOICEMAIL o UNKNOWN.*

**Un producto de [KPBTec](https://github.com/KPBTec) · Knowledge, Protection & Business Technology**

</div>

---

- **Capa 1** — análisis de energía de audio, gratis, <100ms
- **Capa 2** — transcripción con IA (Groq / OpenAI / Together / Deepgram) como fallback, ~300ms

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
- `AMDLAYER`  → `1` (energía) o `2` (Deepgram)
- `AMDMS`     → latencia en ms

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
