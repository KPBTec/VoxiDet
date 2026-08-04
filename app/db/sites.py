"""
db/sites.py — Sedes (agrupación de clientes por ubicación/sucursal).

Feature nueva, no un fix del audit — `clients.site_id` es un INT nullable sin
FOREIGN KEY real (mismo criterio que el resto del repo: ninguna otra tabla usa
constraints de MySQL, las relaciones se manejan a nivel de aplicación, ver
delete_client() en clients.py). Nullable a propósito: clientes existentes
quedan sin sede asignada sin romper nada, asignar una sede es opcional.
"""
from sqlalchemy import text
from app.db.engine import get_db


async def ensure_sites_table() -> None:
    async with get_db() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS sites (
                id         INT AUTO_INCREMENT PRIMARY KEY,
                name       VARCHAR(100) NOT NULL UNIQUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE clients ADD COLUMN site_id INT NULL DEFAULT NULL"
            ))
    except Exception:
        pass  # ya existe


async def list_sites() -> list[dict]:
    async with get_db() as db:
        result = await db.execute(text("SELECT id, name, created_at FROM sites ORDER BY name"))
        return [dict(r) for r in result.mappings().all()]


async def create_site(name: str) -> int:
    async with get_db() as db:
        result = await db.execute(
            text("INSERT INTO sites (name) VALUES (:name)"),
            {"name": name.strip()},
        )
        return result.lastrowid


async def rename_site(site_id: int, name: str) -> None:
    async with get_db() as db:
        await db.execute(
            text("UPDATE sites SET name=:name WHERE id=:id"),
            {"name": name.strip(), "id": site_id},
        )


async def delete_site(site_id: int) -> None:
    """Desasigna la sede de cualquier cliente que la tuviera antes de borrarla
    — mismo criterio manual que delete_client() (sin FK real, sin ON DELETE
    CASCADE/SET NULL de MySQL que se pueda apoyar acá)."""
    async with get_db() as db:
        await db.execute(text("UPDATE clients SET site_id=NULL WHERE site_id=:id"), {"id": site_id})
        await db.execute(text("DELETE FROM sites WHERE id=:id"), {"id": site_id})


async def set_client_site(client_id: int, site_id: int | None) -> None:
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET site_id=:sid WHERE id=:id"),
            {"sid": site_id, "id": client_id},
        )
    from app.db.clients import _invalidate_by_id
    await _invalidate_by_id(client_id)
