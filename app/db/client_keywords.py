import json
import logging

from sqlalchemy import text
from app.db.engine import get_db

log = logging.getLogger("voxidet.client_keywords")


async def ensure_client_keywords_table() -> None:
    async with get_db() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS client_keywords (
                id        INT AUTO_INCREMENT PRIMARY KEY,
                client_id INT NOT NULL,
                word      VARCHAR(100) NOT NULL,
                type      VARCHAR(10)  NOT NULL,
                active    TINYINT(1)   NOT NULL DEFAULT 1,
                UNIQUE KEY uq_ck (client_id, word, type),
                INDEX idx_client (client_id)
            )
        """))


async def get_all_client_keywords(client_id: int) -> list[dict]:
    async with get_db() as db:
        result = await db.execute(
            text("SELECT id, word, type, active FROM client_keywords WHERE client_id=:cid ORDER BY type, word"),
            {"cid": client_id},
        )
        return [dict(r) for r in result.mappings().all()]


async def get_client_keyword_sets(client_id: int) -> tuple[set[str], set[str]]:
    """Devuelve (human_words, voicemail_words) activas para el cliente."""
    async with get_db() as db:
        result = await db.execute(
            text("SELECT word, type FROM client_keywords WHERE client_id=:cid AND active=1"),
            {"cid": client_id},
        )
        human: set[str] = set()
        voicemail: set[str] = set()
        for word, type_ in result.fetchall():
            (human if type_ == "HUMAN" else voicemail).add(word)
    return human, voicemail


async def get_cached_client_keywords(client_id: int) -> tuple[set[str], set[str]]:
    """Keywords del cliente con cache Redis 60s — evita consulta BD por llamada."""
    from app.cache.client_cache import get_redis
    cache_key = f"client_kw:{client_id}"
    try:
        r = await get_redis()
        raw = await r.get(cache_key)
        if raw:
            data = json.loads(raw)
            return set(data["human"]), set(data["voicemail"])
    except Exception:
        pass
    human, voicemail = await get_client_keyword_sets(client_id)
    try:
        r = await get_redis()
        await r.set(cache_key, json.dumps({"human": list(human), "voicemail": list(voicemail)}), ex=60)
    except Exception:
        pass
    return human, voicemail


async def invalidate_client_keywords_cache(client_id: int) -> None:
    from app.cache.client_cache import get_redis
    try:
        r = await get_redis()
        await r.delete(f"client_kw:{client_id}")
    except Exception:
        pass


async def add_client_keyword(client_id: int, word: str, type_: str) -> bool:
    try:
        async with get_db() as db:
            await db.execute(
                text("INSERT IGNORE INTO client_keywords (client_id, word, type) VALUES (:cid, :w, :t)"),
                {"cid": client_id, "w": word.lower().strip(), "t": type_},
            )
        await invalidate_client_keywords_cache(client_id)
        return True
    except Exception:
        return False


async def delete_client_keyword(kw_id: int, client_id: int) -> None:
    async with get_db() as db:
        await db.execute(text("DELETE FROM client_keywords WHERE id=:id"), {"id": kw_id})
    await invalidate_client_keywords_cache(client_id)


async def toggle_client_keyword(kw_id: int, client_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            text("UPDATE client_keywords SET active = 1-active WHERE id=:id"),
            {"id": kw_id},
        )
    await invalidate_client_keywords_cache(client_id)
