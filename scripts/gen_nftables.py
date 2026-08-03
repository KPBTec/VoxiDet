#!/usr/bin/env python3
"""
VoxiDet — Generador de reglas nftables.
Lee DB → aplica nftables. Ejecutado por el panel admin y en cron.

Requiere:
  - nftables instalado (apt install nftables)
  - sudo NOPASSWD para /usr/sbin/nft en /etc/sudoers.d/voxidet
  - DB accesible (usa las mismas variables que el servidor)

Cron (cada 5 min como safety net):
  */5 * * * * root /usr/bin/python3 /opt/voxidet/scripts/gen_nftables.py
"""
import glob
import os
import sys
import subprocess
from pathlib import Path

# Localizar credentials.conf del deploy
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

API_PORT = os.getenv("API_PORT", "8000")

NFTABLES_FRAGMENT = Path("/etc/nftables.d/voxidet.nft")
NFTABLES_MAIN     = Path("/etc/nftables.conf")


def _parse_sshd_ports(path: str, seen: set, ports: list) -> None:
    try:
        content = Path(path).read_text(errors="ignore")
    except (FileNotFoundError, PermissionError):
        return
    for line in content.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        key, value = key.lower(), value.strip()
        if key == "port" and value.isdigit() and value not in seen:
            seen.add(value)
            ports.append(value)
        elif key == "include":
            for inc in sorted(glob.glob(value)):
                _parse_sshd_ports(inc, seen, ports)


def detect_ssh_ports() -> list[str]:
    """Lee /etc/ssh/sshd_config (siguiendo Include) para no bloquear el SSH
    real si el puerto fue cambiado del 22 por defecto (hardening común)."""
    env_override = os.getenv("SSH_PORT", "").strip()
    if env_override:
        return [p.strip() for p in env_override.split(",") if p.strip()]

    seen: set = set()
    ports: list = []
    _parse_sshd_ports("/etc/ssh/sshd_config", seen, ports)
    return ports or ["22"]


SSH_PORTS = detect_ssh_ports()


def get_db():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        database=DB_NAME, charset="utf8mb4",
    )


def _clean_ip(ip: str) -> str:
    return ip.strip().split("/")[0].strip()


def build_nftables(rules: list[dict]) -> str:
    """Genera un FRAGMENTO (sets + reglas sueltas) para incluir dentro del
    chain input de /etc/nftables.conf — mismo patrón que carriers.nft/
    customers.nft de VoxiKam. NO es una tabla aparte: nftables no permite
    un bloque `table` dentro del cuerpo de un `chain`.

    Nota: nftables.conf ya tiene `tcp dport __SSH_PORT__ accept` y
    `tcp dport __WEB_PORT__ accept` incondicionales *antes* de este include
    (SSH abierto a cualquier IP, decisión explícita) — mientras esas reglas
    estáticas sigan ahí, las reglas de SSH/API de este fragmento nunca se
    alcanzan (accept es un veredicto terminal). Quedan generadas por si en
    algún momento se quita el accept incondicional de nftables.conf.
    """
    allow_ssh = [_clean_ip(r["ip"]) for r in rules if r["action"] == "allow" and r["service"] in ("ssh", "all")]
    allow_api = [_clean_ip(r["ip"]) for r in rules if r["action"] == "allow" and r["service"] in ("api", "all")]
    deny_all  = [_clean_ip(r["ip"]) for r in rules if r["action"] == "deny"]

    lines = ["# AUTO-GENERADO por gen_nftables.py — NO editar manualmente", ""]

    if not (deny_all or allow_ssh or allow_api):
        lines.append("# Sin reglas configuradas en el panel")
        return "\n".join(lines)

    # define — no "set": un set de nftables es un objeto que solo puede
    # declararse a nivel de tabla, no dentro de un chain{} (donde queda este
    # fragmento incluido). define es sustitución de texto, válida en
    # cualquier lugar — mismo mecanismo que "define carrier_ips = {...}" en
    # los fragmentos de VoxiKam.
    if deny_all:
        ips = ", ".join(deny_all)
        lines.append(f"define blocked_ips = {{ {ips} }}")
    if allow_ssh:
        ips = ", ".join(allow_ssh)
        lines.append(f"define ssh_allowed = {{ {ips} }}")
    if allow_api:
        ips = ", ".join(allow_api)
        lines.append(f"define api_allowed = {{ {ips} }}")
    lines.append("")

    # Bloqueo de IPs denegadas (prevalece sobre todo)
    if deny_all:
        lines += ["# IPs bloqueadas explicitamente", "ip saddr $blocked_ips drop", ""]

    ssh_ports = "{ " + ", ".join(SSH_PORTS) + " }"
    if allow_ssh:
        lines += [
            "# SSH — solo IPs autorizadas (panel)",
            f"tcp dport {ssh_ports} ip saddr $ssh_allowed accept",
            f"tcp dport {ssh_ports} drop",
            "",
        ]

    if allow_api:
        lines += [
            "# API — solo IPs de la whitelist (panel)",
            f"tcp dport {API_PORT} ip saddr $api_allowed accept",
            f"tcp dport {API_PORT} drop",
            "",
        ]

    return "\n".join(lines)


def apply(nft_content: str) -> bool:
    """Escribe el fragmento y recarga TODO /etc/nftables.conf (flush ruleset +
    estático + este include) — mismo mecanismo que apply_nftables() de
    VoxiKam. Ya no hay una tabla nombrada aparte que borrar: el fragmento es
    parte del chain de la tabla principal, se reemplaza entera cada vez."""
    NFTABLES_FRAGMENT.parent.mkdir(parents=True, exist_ok=True)
    NFTABLES_FRAGMENT.write_text(nft_content)

    # Validar sintaxis del archivo completo (fragmento incluido) antes de aplicar
    val = subprocess.run(
        ["sudo", "nft", "-c", "-f", str(NFTABLES_MAIN)],
        capture_output=True, text=True, timeout=10
    )
    if val.returncode != 0:
        print(f"  ERROR sintaxis nftables:\n{val.stderr.strip()}", file=sys.stderr)
        return False

    result = subprocess.run(
        ["sudo", "nft", "-f", str(NFTABLES_MAIN)],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        print("  nftables aplicado correctamente")
        return True
    else:
        print(f"  ERROR aplicando nftables:\n{result.stderr.strip()}", file=sys.stderr)
        return False


def main():
    try:
        conn = get_db()
        cur  = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT ip, action, service FROM firewall_rules WHERE active=1")
        rules = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"  ERROR leyendo DB: {e}", file=sys.stderr)
        sys.exit(1)

    nft = build_nftables(rules)
    print(f"  {len([r for r in rules if r['action']=='deny'])} reglas deny, "
          f"{len([r for r in rules if r['action']=='allow'])} allow")
    ok = apply(nft)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
