#!/usr/bin/env python3
"""
cli/manage.py — Gestión de clientes VoxiDet desde línea de comandos.

Ejecutar dentro del contenedor:
  docker exec -it voxidet-api python cli/manage.py add-client "Nombre" --limit 500000
  docker exec -it voxidet-api python cli/manage.py list-clients
  docker exec -it voxidet-api python cli/manage.py set-ips
  docker exec -it voxidet-api python cli/manage.py stats
"""

import sys
import os
import secrets
import asyncio
import argparse

# Permite importar desde app/ cuando se ejecuta como script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.db.engine import AsyncSessionLocal as Session
from app.cache.client_cache import invalidate_api_key
from app.config import settings

Session = Session  # alias explícito para claridad


def _gen_key() -> str:
    return secrets.token_urlsafe(36)


async def cmd_add_client(name: str, limit: int, ips: str = "", notes: str = ""):
    api_key       = _gen_key()
    install_token = _gen_key()

    async with Session() as db:
        await db.execute(
            text("""
                INSERT INTO clients
                    (name, api_key, install_token, active, daily_limit, allowed_ips, notes)
                VALUES (:name, :key, :token, 1, :limit, :ips, :notes)
            """),
            {
                "name": name, "key": api_key, "token": install_token,
                "limit": limit, "ips": ips or None, "notes": notes,
            },
        )
        await db.commit()

    public_url = settings.PUBLIC_URL.rstrip("/")
    print(f"\n✅ Cliente creado")
    print(f"   Nombre        : {name}")
    print(f"   API Key       : {api_key}")
    print(f"   Install Token : {install_token}")
    print(f"   Límite/día    : {limit:,} llamadas")
    print(f"   IPs permitidas: {ips or '(sin restricción)'}")
    print(f"\n📦 Instalación en cada nodo Asterisk:")
    print(f"   wget {public_url}/install/{install_token} -O /var/lib/asterisk/agi-bin/amd_ia.agi")
    print(f"   chmod 755 /var/lib/asterisk/agi-bin/amd_ia.agi")
    print(f"\n⚠️  Guarda estos tokens — no se pueden recuperar después.\n")


async def cmd_list_clients():
    async with Session() as db:
        result = await db.execute(text("""
            SELECT c.id, c.name, c.active, c.daily_limit, c.allowed_ips, c.created_at,
                   COALESCE(u.total_calls, 0) AS today_calls
            FROM clients c
            LEFT JOIN daily_usage u ON u.client_id = c.id AND u.date = CURDATE()
            ORDER BY c.id
        """))
        rows = result.mappings().all()

    if not rows:
        print("No hay clientes registrados.")
        return

    print(f"\n{'ID':>4}  {'Nombre':<25}  {'Estado':>8}  {'Límite/día':>12}  {'Hoy':>8}  {'IPs'}")
    print("─" * 90)
    for r in rows:
        estado = "✅ ACTIVO" if r["active"] else "❌ INACT."
        ips    = r["allowed_ips"] or "cualquier IP"
        print(f"{r['id']:>4}  {r['name']:<25}  {estado:>8}  {r['daily_limit']:>12,}  {r['today_calls']:>8,}  {ips}")
    print()


async def cmd_deactivate_client(client_id: int):
    async with Session() as db:
        await db.execute(text("UPDATE clients SET active=0 WHERE id=:id"), {"id": client_id})
        await db.commit()
    print(f"✅ Cliente {client_id} desactivado.")


async def cmd_activate_client(client_id: int):
    async with Session() as db:
        await db.execute(text("UPDATE clients SET active=1 WHERE id=:id"), {"id": client_id})
        await db.commit()
    print(f"✅ Cliente {client_id} activado.")


async def cmd_reset_key(client_id: int):
    new_key = _gen_key()
    async with Session() as db:
        result = await db.execute(
            text("SELECT api_key FROM clients WHERE id=:id"), {"id": client_id}
        )
        row = result.first()
        if not row:
            print(f"❌ Cliente {client_id} no encontrado.")
            return
        await db.execute(
            text("UPDATE clients SET api_key=:key WHERE id=:id"), {"key": new_key, "id": client_id}
        )
        await db.commit()

    await invalidate_api_key(row[0])
    print(f"\n✅ API Key renovada para cliente {client_id}")
    print(f"   Nueva API Key: {new_key}")
    print(f"\n⚠️  Actualiza la key en el AGI del cliente.\n")


async def cmd_reset_install_token(client_id: int):
    new_token = _gen_key()
    async with Session() as db:
        result = await db.execute(
            text("SELECT name FROM clients WHERE id=:id"), {"id": client_id}
        )
        row = result.first()
        if not row:
            print(f"❌ Cliente {client_id} no encontrado.")
            return
        await db.execute(
            text("UPDATE clients SET install_token=:token WHERE id=:id"),
            {"token": new_token, "id": client_id},
        )
        await db.commit()

    public_url = settings.PUBLIC_URL.rstrip("/")
    print(f"\n✅ Install token renovado para cliente {client_id} ({row[0]})")
    print(f"   wget {public_url}/install/{new_token} -O /var/lib/asterisk/agi-bin/amd_ia.agi")
    print(f"\n⚠️  El AGI ya instalado sigue funcionando con el token anterior.\n")


async def cmd_set_ips():
    async with Session() as db:
        result = await db.execute(
            text("SELECT id, name, active, allowed_ips FROM clients ORDER BY id")
        )
        clients = result.mappings().all()

    if not clients:
        print("No hay clientes registrados.")
        return

    print("\n═══ Clientes registrados ═══")
    print(f"{'#':>3}  {'Nombre':<28}  {'Estado':>8}  IPs permitidas")
    print("─" * 75)
    for i, c in enumerate(clients, 1):
        estado = "ACTIVO" if c["active"] else "INACTIVO"
        ips    = c["allowed_ips"] or "(sin restricción)"
        print(f"{i:>3}  {c['name']:<28}  {estado:>8}  {ips}")
    print()

    try:
        sel = int(input("Selecciona el número del cliente: ").strip())
        if sel < 1 or sel > len(clients):
            print("❌ Número inválido.")
            return
    except (ValueError, EOFError):
        print("❌ Entrada inválida.")
        return

    chosen = clients[sel - 1]
    print(f"\nCliente      : {chosen['name']}")
    print(f"IPs actuales : {chosen['allowed_ips'] or '(sin restricción)'}")
    print("Formato      : 190.1.2.3  |  190.1.2.3,190.1.2.4  |  190.0.0.0/24")
    print("(Enter para quitar restricción)\n")

    try:
        new_ips = input("Nuevas IPs: ").strip()
    except EOFError:
        new_ips = ""

    clean = new_ips if new_ips.lower() not in ("", "none", "null") else ""

    async with Session() as db:
        await db.execute(
            text("UPDATE clients SET allowed_ips=:ips WHERE id=:id"),
            {"ips": clean or None, "id": chosen["id"]},
        )
        await db.commit()

    if clean:
        print(f"\n✅ '{chosen['name']}' → IPs: {clean}\n")
    else:
        print(f"\n✅ '{chosen['name']}' → sin restricción de IP\n")


async def cmd_stats(client_id: int | None):
    where  = "WHERE u.client_id = :cid" if client_id else ""
    params = {"cid": client_id} if client_id else {}

    async with Session() as db:
        result = await db.execute(text(f"""
            SELECT c.name, u.date, u.total_calls,
                   u.human_count, u.voicemail_count, u.unknown_count, u.deepgram_calls
            FROM daily_usage u
            JOIN clients c ON c.id = u.client_id
            {where}
            ORDER BY u.date DESC, c.name
            LIMIT 30
        """), params)
        rows = result.mappings().all()

    if not rows:
        print("Sin datos.")
        return

    print(f"\n{'Cliente':<25}  {'Fecha':>10}  {'Total':>8}  {'Human':>7}  {'VM':>7}  {'Unk':>5}  {'DG':>5}")
    print("─" * 80)
    for r in rows:
        print(
            f"{r['name']:<25}  {str(r['date']):>10}  {r['total_calls']:>8,}  "
            f"{r['human_count']:>7,}  {r['voicemail_count']:>7,}  "
            f"{r['unknown_count']:>5,}  {r['deepgram_calls']:>5,}"
        )
    print()


def main():
    parser = argparse.ArgumentParser(description="VoxiDet — Gestión de clientes")
    sub    = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add-client")
    p_add.add_argument("name")
    p_add.add_argument("--limit", type=int, default=50000)
    p_add.add_argument("--ips",   default="")
    p_add.add_argument("--notes", default="")

    sub.add_parser("list-clients")

    p_deact = sub.add_parser("deactivate-client")
    p_deact.add_argument("id", type=int)

    p_act = sub.add_parser("activate-client")
    p_act.add_argument("id", type=int)

    p_reset = sub.add_parser("reset-key")
    p_reset.add_argument("id", type=int)

    p_rit = sub.add_parser("reset-install-token")
    p_rit.add_argument("id", type=int)

    sub.add_parser("set-ips")

    p_stats = sub.add_parser("stats")
    p_stats.add_argument("--client-id", type=int, default=None)

    args = parser.parse_args()

    dispatch = {
        "add-client":          lambda: cmd_add_client(args.name, args.limit, args.ips, args.notes),
        "list-clients":        lambda: cmd_list_clients(),
        "deactivate-client":   lambda: cmd_deactivate_client(args.id),
        "activate-client":     lambda: cmd_activate_client(args.id),
        "reset-key":           lambda: cmd_reset_key(args.id),
        "reset-install-token": lambda: cmd_reset_install_token(args.id),
        "set-ips":             lambda: cmd_set_ips(),
        "stats":               lambda: cmd_stats(args.client_id),
    }

    if args.command in dispatch:
        asyncio.run(_run(dispatch[args.command]()))
    else:
        parser.print_help()


async def _run(coro):
    try:
        await coro
    finally:
        from app.db.engine import engine
        await engine.dispose()


if __name__ == "__main__":
    main()
