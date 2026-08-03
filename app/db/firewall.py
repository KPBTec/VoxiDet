from sqlalchemy import text
from app.db.engine import get_db


async def ensure_firewall_table() -> None:
    async with get_db() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS firewall_rules (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                ip          VARCHAR(64)  NOT NULL,
                action      ENUM('allow','deny') NOT NULL DEFAULT 'allow',
                service     ENUM('all','api','ssh')  NOT NULL DEFAULT 'all',
                description VARCHAR(200),
                active      TINYINT(1) NOT NULL DEFAULT 1,
                created_at  DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))


async def list_rules() -> list[dict]:
    async with get_db() as db:
        r = await db.execute(text(
            "SELECT * FROM firewall_rules ORDER BY action DESC, service, ip"
        ))
        return [dict(row) for row in r.mappings().all()]


async def add_rule(ip: str, action: str, service: str, description: str) -> int:
    async with get_db() as db:
        r = await db.execute(text("""
            INSERT INTO firewall_rules (ip, action, service, description)
            VALUES (:ip, :action, :service, :desc)
        """), {"ip": ip, "action": action, "service": service, "desc": description or None})
        return r.lastrowid


async def toggle_rule(rule_id: int) -> bool:
    async with get_db() as db:
        r = await db.execute(
            text("SELECT active FROM firewall_rules WHERE id=:id"), {"id": rule_id}
        )
        row = r.first()
        if not row:
            return False
        new = 0 if row[0] else 1
        await db.execute(
            text("UPDATE firewall_rules SET active=:a WHERE id=:id"),
            {"a": new, "id": rule_id},
        )
        return bool(new)


async def delete_rule(rule_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            text("DELETE FROM firewall_rules WHERE id=:id"), {"id": rule_id}
        )


async def get_active_rules() -> list[dict]:
    async with get_db() as db:
        r = await db.execute(text(
            "SELECT ip, action, service FROM firewall_rules WHERE active=1"
        ))
        return [dict(row) for row in r.mappings().all()]


async def ensure_fail2ban_unban_table() -> None:
    """El panel corre en un contenedor Docker y no puede llamar fail2ban-client
    en el host directo (mismo motivo por el que gen_nftables.py corre por cron,
    no sincrónico) — la solicitud se encola aquí y un script de host la procesa."""
    async with get_db() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS fail2ban_unban_requests (
                id           INT AUTO_INCREMENT PRIMARY KEY,
                jail         VARCHAR(64) NOT NULL,
                ip           VARCHAR(64) NOT NULL,
                requested_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))


async def queue_unban(jail: str, ip: str) -> None:
    async with get_db() as db:
        await db.execute(text("""
            INSERT INTO fail2ban_unban_requests (jail, ip) VALUES (:jail, :ip)
        """), {"jail": jail, "ip": ip})
