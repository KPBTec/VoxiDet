"""
Configuración editable desde el panel, persistida en MySQL — no en
credentials.conf. El contenedor `api` recibe ese archivo vía `env_file:` en
docker-compose.yml, que Compose lee al crear el contenedor e inyecta como
env vars; nunca lo monta como archivo dentro del contenedor, así que la app
no tiene forma de reescribirlo (mismo motivo por el que VoxiKam nunca pudo
reescribir su propio credentials.conf para desactivar keys legacy, v1.16.1).
Cualquier configuración que el panel necesite modificar en runtime vive acá.
"""
from sqlalchemy import text
from app.db.engine import get_db


async def ensure_app_settings_table() -> None:
    async with get_db() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS app_settings (
                `key`      VARCHAR(50) PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """))


async def get_setting(key: str) -> str | None:
    async with get_db() as db:
        result = await db.execute(text("SELECT value FROM app_settings WHERE `key`=:k"), {"k": key})
        row = result.first()
        return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    async with get_db() as db:
        await db.execute(text("""
            INSERT INTO app_settings (`key`, value) VALUES (:k, :v)
            ON DUPLICATE KEY UPDATE value = :v
        """), {"k": key, "v": value})
