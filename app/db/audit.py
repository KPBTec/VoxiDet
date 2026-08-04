"""
app/db/audit.py — historial de cambios por cliente. `users.html` ya decía
textualmente que la auditoría de cambios "dice quién hizo qué" — no existía
ninguna tabla ni código detrás de esa promesa hasta este módulo.
"""
import logging

from sqlalchemy import text
from app.db.engine import get_db

log = logging.getLogger("voxidet.audit")


async def ensure_audit_log_table() -> None:
    async with get_db() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                admin_user    VARCHAR(50)  NOT NULL,
                client_id     INT          NOT NULL,
                field_changed VARCHAR(50)  NOT NULL,
                old_value     TEXT         DEFAULT NULL,
                new_value     TEXT         DEFAULT NULL,
                created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_client (client_id, created_at)
            )
        """))


async def log_audit(admin_user: str, client_id: int, field: str, old_value, new_value) -> None:
    """Nunca debe romper la acción que audita — un fallo acá se loguea y se
    ignora, mismo criterio que save_log() (app/db/logs.py)."""
    try:
        async with get_db() as db:
            await db.execute(text("""
                INSERT INTO audit_log (admin_user, client_id, field_changed, old_value, new_value)
                VALUES (:u, :cid, :f, :old, :new)
            """), {
                "u": admin_user or "?",
                "cid": client_id,
                "f": field,
                "old": str(old_value) if old_value is not None else None,
                "new": str(new_value) if new_value is not None else None,
            })
    except Exception:
        log.exception("log_audit falló (client_id=%s field=%s) — cambio real aplicado igual, solo se perdió el registro", client_id, field)


async def get_client_audit_log(client_id: int, limit: int = 30) -> list[dict]:
    async with get_db() as db:
        result = await db.execute(text("""
            SELECT admin_user, field_changed, old_value, new_value, created_at
            FROM audit_log WHERE client_id=:cid ORDER BY created_at DESC LIMIT :lim
        """), {"cid": client_id, "lim": limit})
        return [dict(r) for r in result.mappings().all()]
