"""
usage_sync.py — vuelca contadores de Redis a MySQL cada 15s (daily_usage y
provider_stats).

Las detecciones incrementan contadores en Redis (app/cache/client_cache.py:
record_daily_usage / record_provider_stat, HINCRBY — sin contención de locks,
ver esas funciones para el motivo). Este módulo periódicamente lee esos
contadores y los ESCRIBE como valores absolutos en MySQL (no incrementa) —
así, aunque varios workers corran este mismo loop en paralelo, escribir el
mismo valor dos veces no duplica nada (idempotente), sin necesitar
coordinación entre workers.
"""
import asyncio
import logging

from sqlalchemy import text

from app.cache.client_cache import get_redis
from app.db.engine import get_db

log = logging.getLogger("voxidet.usage_sync")

_FIELDS = ("total_calls", "human_count", "voicemail_count", "unknown_count", "deepgram_calls")


async def _sync_once() -> None:
    try:
        r = await get_redis()
        keys = [k async for k in r.scan_iter(match="amd:usage:*", count=200)]
        if not keys:
            return
        for key in keys:
            # amd:usage:{client_id}:{YYYY-MM-DD}
            try:
                _, _, client_id_s, date_s = key.split(":", 3)
                client_id = int(client_id_s)
            except (ValueError, IndexError):
                continue
            data = await r.hgetall(key)
            if not data:
                continue
            values = {f: int(data.get(f, 0) or 0) for f in _FIELDS}
            async with get_db() as db:
                await db.execute(
                    text("""
                        INSERT INTO daily_usage
                            (client_id, date, total_calls, human_count, voicemail_count, unknown_count, deepgram_calls)
                        VALUES
                            (:cid, :date, :total, :human, :voicemail, :unknown, :deepgram)
                        ON DUPLICATE KEY UPDATE
                            total_calls     = :total,
                            human_count     = :human,
                            voicemail_count = :voicemail,
                            unknown_count   = :unknown,
                            deepgram_calls  = :deepgram
                    """),
                    {
                        "cid": client_id, "date": date_s,
                        "total": values["total_calls"], "human": values["human_count"],
                        "voicemail": values["voicemail_count"], "unknown": values["unknown_count"],
                        "deepgram": values["deepgram_calls"],
                    },
                )
    except Exception as e:
        log.warning("usage_sync error (daily_usage): %s", e)


_PSTAT_FIELDS = ("requests", "errors", "audio_ms")


async def _sync_provider_stats_once() -> None:
    try:
        r = await get_redis()
        keys = [k async for k in r.scan_iter(match="amd:pstat:*", count=200)]
        if not keys:
            return
        for key in keys:
            # amd:pstat:{provider}:{key_idx}:{YYYY-MM-DD}
            try:
                _, _, provider, key_idx_s, date_s = key.split(":", 4)
                key_idx = int(key_idx_s)
            except (ValueError, IndexError):
                continue
            data = await r.hgetall(key)
            if not data:
                continue
            values = {f: int(data.get(f, 0) or 0) for f in _PSTAT_FIELDS}
            async with get_db() as db:
                await db.execute(
                    text("""
                        INSERT INTO provider_stats (provider, key_idx, date, requests, errors, audio_ms)
                        VALUES (:p, :k, :date, :requests, :errors, :audio_ms)
                        ON DUPLICATE KEY UPDATE
                            requests = :requests,
                            errors   = :errors,
                            audio_ms = :audio_ms
                    """),
                    {
                        "p": provider, "k": key_idx, "date": date_s,
                        "requests": values["requests"], "errors": values["errors"],
                        "audio_ms": values["audio_ms"],
                    },
                )
    except Exception as e:
        log.warning("usage_sync error (provider_stats): %s", e)


async def start() -> None:
    asyncio.create_task(_loop())


async def _loop() -> None:
    while True:
        await asyncio.sleep(15)
        await _sync_once()
        await _sync_provider_stats_once()
