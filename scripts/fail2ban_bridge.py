#!/usr/bin/env python3
"""
VoxiDet — Puente fail2ban <-> panel admin.

El panel corre dentro de un contenedor Docker y no puede llamar
`fail2ban-client` en el host directo (mismo motivo por el que
gen_nftables.py corre por cron, no sincrónico desde la app). Este script
corre en el host (cron cada minuto) y hace dos cosas:

  1. Procesa solicitudes de "unban" encoladas por el panel en la tabla
     fail2ban_unban_requests, ejecuta el unban real, borra la fila.
  2. Vuelca el estado actual (IPs baneadas por jail) a un JSON en el
     directorio de logs — el mismo bind mount que ya usa /srv/logs, así que
     el panel (dentro del contenedor) lo puede leer sin necesitar acceso al
     host.

Cron (cada minuto):
  * * * * * root /usr/bin/python3 /opt/voxidet/scripts/fail2ban_bridge.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# Localizar credentials.conf del deploy — mismo mecanismo que gen_nftables.py
_marker = Path("/etc/voxidet.conf")
if _marker.exists():
    for _line in _marker.read_text().splitlines():
        if _line.startswith("INSTALL_DIR="):
            _install = Path(_line.split("=", 1)[1].strip())
            break
    else:
        _install = Path("/opt/voxidet")
else:
    _install = Path("/opt/voxidet")

_env_file = _install / "credentials.conf"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            k, _, v = _line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import pymysql

DB_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("MYSQL_PORT", "3306"))
DB_USER = os.getenv("MYSQL_USER", "voxidet")
DB_PASS = os.getenv("MYSQL_PASSWORD", "")
DB_NAME = os.getenv("MYSQL_DATABASE", "voxidet_db")

JAILS = ["sshd", "voxidet-security"]

# Bind mount compartido con el contenedor (docker-compose.yml: /opt/voxidet/logs:/srv/logs)
STATUS_FILE = Path("/opt/voxidet/logs/fail2ban-status.json")


def get_db():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        database=DB_NAME, charset="utf8mb4",
    )


def _parse_banned_ips(status_output: str) -> list[str]:
    for line in status_output.splitlines():
        line = line.strip()
        if "Banned IP list:" in line:
            ips = line.split(":", 1)[1].strip()
            return ips.split() if ips else []
    return []


def process_unban_queue() -> int:
    conn = get_db()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT id, jail, ip FROM fail2ban_unban_requests")
    rows = cur.fetchall()

    for row in rows:
        if row["jail"] in JAILS:
            subprocess.run(
                ["sudo", "fail2ban-client", "set", row["jail"], "unbanip", row["ip"]],
                capture_output=True, timeout=5,
            )
        cur.execute("DELETE FROM fail2ban_unban_requests WHERE id=%s", (row["id"],))

    conn.commit()
    cur.close()
    conn.close()
    return len(rows)


def dump_status() -> None:
    status = {}
    for jail in JAILS:
        try:
            p = subprocess.run(
                ["sudo", "fail2ban-client", "status", jail],
                capture_output=True, text=True, timeout=5,
            )
            status[jail] = _parse_banned_ips(p.stdout) if p.returncode == 0 else []
        except Exception:
            status[jail] = []

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status))


def main():
    try:
        n = process_unban_queue()
        if n:
            print(f"  {n} solicitud(es) de unban procesadas")
    except Exception as e:
        print(f"  ERROR procesando cola de unban: {e}", file=sys.stderr)

    try:
        dump_status()
    except Exception as e:
        print(f"  ERROR volcando estado fail2ban: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
