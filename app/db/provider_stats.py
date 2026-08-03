from datetime import date, timedelta
from sqlalchemy import text
from app.db.engine import get_db
from app.cache.client_cache import record_provider_stat

# Precio por minuto de audio por proveedor (modelo por defecto)
PRICE_PER_MIN: dict[str, float] = {
    "groq":        0.00067,   # $0.04/h
    "deepgram":    0.0043,
    "deepgramv2":  0.0043,
    "fireworks":   0.0020,
    "together":    0.0015,
    "openai":      0.0030,    # gpt-4o-mini-transcribe
    "vosk":        0.0,
    "vosk_stream": 0.0,
    "sherpa":      0.0,
}

# Unidad mínima de facturación por proveedor
BILLING_UNIT: dict[str, str] = {
    "groq":        "mín. 10s",
    "deepgram":    "por segundo",
    "deepgramv2":  "por segundo",
    "fireworks":   "por segundo",
    "together":    "por segundo real",
    "openai":      "por segundo",
    "vosk":        "local",
    "vosk_stream": "local",
    "sherpa":      "local",
}


async def ensure_provider_stats_table() -> None:
    async with get_db() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS provider_stats (
                provider  VARCHAR(20) NOT NULL,
                key_idx   TINYINT     NOT NULL DEFAULT 0,
                date      DATE        NOT NULL,
                requests  INT         NOT NULL DEFAULT 0,
                errors    INT         NOT NULL DEFAULT 0,
                audio_ms  BIGINT      NOT NULL DEFAULT 0,
                PRIMARY KEY (provider, key_idx, date)
            )
        """))


async def record_stat(
    provider: str, key_idx: int, audio_ms: int, error: bool = False
) -> None:
    # Redis (sin contención de locks) — ver client_cache.record_provider_stat
    # y app/core/usage_sync.py, que vuelca esto a MySQL cada 15s.
    await record_provider_stat(provider, key_idx, audio_ms, error)


async def get_summary(period_days: int = 1) -> list[dict]:
    """Stats agregadas por proveedor para los últimos N días."""
    since = date.today() - timedelta(days=period_days - 1)
    async with get_db() as db:
        result = await db.execute(text("""
            SELECT provider,
                   SUM(requests) AS requests,
                   SUM(errors)   AS errors,
                   SUM(audio_ms) AS audio_ms
            FROM provider_stats
            WHERE date >= :since
            GROUP BY provider
            ORDER BY requests DESC
        """), {"since": since})
        rows = result.mappings().all()
    out = []
    for r in rows:
        provider  = r["provider"]
        audio_min = float(r["audio_ms"] or 0) / 60000
        price = PRICE_PER_MIN.get(provider, 0.0)
        out.append({
            "provider":     provider,
            "requests":     int(r["requests"] or 0),
            "errors":       int(r["errors"]   or 0),
            "audio_ms":     int(r["audio_ms"] or 0),
            "audio_min":    round(audio_min, 2),
            "cost_usd":     round(audio_min * price, 4),
            "price_per_min": price,
            "billing_unit": BILLING_UNIT.get(provider, "por segundo"),
            "is_local":     price == 0.0,
        })
    return out


async def get_by_key(period_days: int = 1) -> list[dict]:
    """Stats por proveedor+key para los últimos N días."""
    since = date.today() - timedelta(days=period_days - 1)
    async with get_db() as db:
        result = await db.execute(text("""
            SELECT provider, key_idx,
                   SUM(requests) AS requests,
                   SUM(errors)   AS errors,
                   SUM(audio_ms) AS audio_ms
            FROM provider_stats
            WHERE date >= :since
            GROUP BY provider, key_idx
            ORDER BY provider, key_idx
        """), {"since": since})
        rows = result.mappings().all()
    out = []
    for r in rows:
        provider  = r["provider"]
        audio_min = float(r["audio_ms"] or 0) / 60000
        out.append({
            "provider": provider,
            "key_idx":  int(r["key_idx"]),
            "requests": int(r["requests"] or 0),
            "errors":   int(r["errors"]   or 0),
            "audio_ms": int(r["audio_ms"] or 0),
            "audio_min": round(audio_min, 2),
            "cost_usd": round(audio_min * PRICE_PER_MIN.get(provider, 0.0), 4),
        })
    return out
