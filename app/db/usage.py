from sqlalchemy import text
from app.db.engine import get_db


async def get_daily_stats(client_id: int) -> dict:
    async with get_db() as db:
        result = await db.execute(
            text("""
                SELECT date, total_calls, human_count, voicemail_count,
                       unknown_count, deepgram_calls
                FROM daily_usage
                WHERE client_id = :cid AND date = CURDATE()
                LIMIT 1
            """),
            {"cid": client_id},
        )
        row = result.mappings().first()
        return dict(row) if row else {}


async def get_today_summary() -> dict:
    """Agregado de daily_usage de hoy, todos los clientes — para el Dashboard."""
    async with get_db() as db:
        result = await db.execute(text("""
            SELECT
                COALESCE(SUM(total_calls), 0)     AS total_calls,
                COALESCE(SUM(human_count), 0)     AS human_count,
                COALESCE(SUM(voicemail_count), 0) AS voicemail_count,
                COALESCE(SUM(unknown_count), 0)   AS unknown_count
            FROM daily_usage
            WHERE date = CURDATE()
        """))
        row = result.mappings().first()
        return dict(row) if row else {
            "total_calls": 0, "human_count": 0, "voicemail_count": 0, "unknown_count": 0,
        }


async def get_all_daily_stats() -> list[dict]:
    async with get_db() as db:
        result = await db.execute(text("""
            SELECT c.name, u.date, u.total_calls,
                   u.human_count, u.voicemail_count, u.unknown_count, u.deepgram_calls
            FROM daily_usage u
            JOIN clients c ON c.id = u.client_id
            WHERE u.date = CURDATE()
            ORDER BY u.total_calls DESC
        """))
        rows = result.mappings().all()
    return [dict(r) for r in rows]
