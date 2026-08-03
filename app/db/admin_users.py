import logging

import bcrypt
from sqlalchemy import text

from app.config import settings
from app.db.engine import get_db

log = logging.getLogger("voxidet.admin_users")


async def ensure_admin_users_table() -> None:
    """Migración: crea admin_users y, si está vacía, la siembra con el admin
    único que hoy vive en credentials.conf (ADMIN_USER/ADMIN_PASSWORD) — así
    nadie queda afuera al actualizar. Desde acá en adelante, esta tabla es la
    fuente de verdad; ADMIN_USER/ADMIN_PASSWORD solo se leen para la siembra
    inicial."""
    async with get_db() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS admin_users (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                username      VARCHAR(50)  NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                active        TINYINT(1)   NOT NULL DEFAULT 1,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login_at TIMESTAMP NULL DEFAULT NULL
            )
        """))
        result = await db.execute(text("SELECT COUNT(*) FROM admin_users"))
        count = result.scalar() or 0
        if count == 0 and settings.ADMIN_USER and settings.ADMIN_PASSWORD:
            pw_hash = bcrypt.hashpw(settings.ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
            # INSERT IGNORE (no INSERT liso): con --preload + N workers de gunicorn, el
            # lifespan de cada worker corre esta migración en paralelo al arrancar — todos
            # leen count==0 antes de que el primero comitee, y sin IGNORE los demás revientan
            # con IntegrityError (username UNIQUE) y gunicorn los mata ("Application startup
            # failed. Exiting."). Mismo patrón que ensure_provider_settings_table() y
            # ensure_client_keywords_table(), que ya usan INSERT IGNORE por esto mismo.
            await db.execute(
                text("INSERT IGNORE INTO admin_users (username, password_hash) VALUES (:u, :p)"),
                {"u": settings.ADMIN_USER, "p": pw_hash},
            )
            log.info("admin_users: sembrado admin inicial '%s' desde credentials.conf", settings.ADMIN_USER)


async def get_admin_by_username(username: str) -> dict | None:
    async with get_db() as db:
        result = await db.execute(
            text("SELECT id, username, active FROM admin_users WHERE username=:u"),
            {"u": username.strip()},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def verify_admin_password(username: str, password: str) -> dict | None:
    """Devuelve el admin si username/password son válidos y está activo, si no None."""
    async with get_db() as db:
        result = await db.execute(
            text("SELECT id, username, password_hash, active FROM admin_users WHERE username=:u"),
            {"u": username},
        )
        row = result.mappings().first()
    if not row or not row["active"]:
        return None
    try:
        if not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
            return None
    except ValueError:
        return None  # hash corrupto/formato viejo — mismo resultado que password incorrecta
    async with get_db() as db:
        await db.execute(
            text("UPDATE admin_users SET last_login_at=NOW() WHERE id=:id"),
            {"id": row["id"]},
        )
    return {"id": row["id"], "username": row["username"]}


async def list_admin_users() -> list[dict]:
    async with get_db() as db:
        result = await db.execute(text(
            "SELECT id, username, active, created_at, last_login_at FROM admin_users ORDER BY id"
        ))
        return [dict(r) for r in result.mappings().all()]


async def count_active_admins() -> int:
    async with get_db() as db:
        result = await db.execute(text("SELECT COUNT(*) FROM admin_users WHERE active=1"))
        return result.scalar() or 0


async def create_admin_user(username: str, password: str) -> int:
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    async with get_db() as db:
        result = await db.execute(
            text("INSERT INTO admin_users (username, password_hash) VALUES (:u, :p)"),
            {"u": username.strip(), "p": pw_hash},
        )
        return result.lastrowid


async def set_admin_active(admin_id: int, active: bool) -> bool:
    """Devuelve False si la operación se rechazó (no se puede desactivar al último admin activo)."""
    if not active and await count_active_admins() <= 1:
        return False
    async with get_db() as db:
        await db.execute(
            text("UPDATE admin_users SET active=:a WHERE id=:id"),
            {"a": 1 if active else 0, "id": admin_id},
        )
    return True


async def update_admin_password(admin_id: int, password: str) -> None:
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    async with get_db() as db:
        await db.execute(
            text("UPDATE admin_users SET password_hash=:p WHERE id=:id"),
            {"p": pw_hash, "id": admin_id},
        )
