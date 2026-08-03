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
