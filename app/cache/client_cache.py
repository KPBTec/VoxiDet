import json
import logging
from datetime import datetime, timedelta

import redis.asyncio as aioredis

from app.config import settings

log = logging.getLogger("voxidet.cache")

_pool: aioredis.Redis | None = None


def _seconds_until_midnight() -> int:
    now = datetime.now()
    eod = datetime(now.year, now.month, now.day) + timedelta(days=1)
    return max(int((eod - now).total_seconds()), 1)


async def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=settings.REDIS_PASSWORD,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        log.info("Redis pool inicializado en %s:%s", settings.REDIS_HOST, settings.REDIS_PORT)
    return _pool


async def invalidate_api_key(api_key: str) -> None:
    try:
        r = await get_redis()
        await r.delete(f"amd:client:{api_key}")
        log.info("Cache invalidado para api_key=%s****", api_key[:8])
    except Exception as e:
        log.warning("Redis invalidate error: %s", e)


# ── Registro completo de cliente (auth) ──────────────────────────────────────
# verify_client()/_auth() (deps.py, stream.py) consultaban get_client_by_apikey
# (MySQL) en CADA request autenticado — el cache de arriba (get_client_id_*)
# solo se usaba para decidir si ESCRIBIR el cache, nunca para saltarse la
# consulta a MySQL. Con 150-200 agentes eso es una query de MySQL por cada
# detección solo para autenticar. Este cache sí evita la consulta en el caso
# común (cache hit).
async def get_client_cached(api_key: str) -> dict | None:
    try:
        r = await get_redis()
        cached = await r.get(f"amd:client:{api_key}")
        if cached is not None:
            return json.loads(cached)
    except Exception as e:
        log.warning("Redis get_client_cached error: %s", e)

    from app.db.clients import get_client_by_apikey   # import perezoso: evita ciclo db<->cache
    client = await get_client_by_apikey(api_key)
    if client is not None:
        try:
            r = await get_redis()
            await r.setex(f"amd:client:{api_key}", settings.REDIS_CACHE_TTL, json.dumps(client, default=str))
        except Exception as e:
            log.warning("Redis set_client_cache error: %s", e)
    return client


async def check_and_increment_limit(client_id: int, daily_limit: int) -> bool:
    if daily_limit == 0:
        return True
    try:
        r = await get_redis()
        key = f"amd:limit:{client_id}"
        current = await r.incr(key)
        if current == 1:
            await r.expire(key, _seconds_until_midnight())
        if current > daily_limit:
            log.warning("Cliente %s excedió límite diario (%s)", client_id, daily_limit)
            return False
        return True
    except Exception as e:
        log.error("Redis check_limit error: %s — fail-open", e)
        return True


async def get_daily_count(client_id: int) -> int:
    try:
        r = await get_redis()
        val = await r.get(f"amd:limit:{client_id}")
        return int(val) if val else 0
    except Exception:
        return 0


async def ping_redis() -> bool:
    try:
        r = await get_redis()
        return await r.ping()
    except Exception:
        return False


# ── Contador de uso diario (dashboard) ───────────────────────────────────────
# Antes cada detección hacía CALL increment_usage(...) en MySQL — un
# INSERT...ON DUPLICATE KEY UPDATE sobre una sola fila (client_id, hoy).
# Con 150-200 agentes y ratio ~5, todas las detecciones del MISMO cliente en
# el MISMO día compiten por el lock de esa fila en InnoDB — se serializan.
# Redis HINCRBY no tiene ese problema: es de un solo hilo y procesa cada
# comando en microsegundos, sin esperar locks entre llamadas concurrentes.
# app/core/usage_sync.py vuelca estos contadores a MySQL cada 15s para el
# dashboard, en vez de tocar MySQL en cada detección.
def _usage_key(client_id: int) -> str:
    return f"amd:usage:{client_id}:{datetime.now().strftime('%Y-%m-%d')}"


async def record_daily_usage(client_id: int, result: str, layer: int) -> None:
    try:
        r = await get_redis()
        key = _usage_key(client_id)
        async with r.pipeline(transaction=False) as pipe:
            pipe.hincrby(key, "total_calls", 1)
            if result == "HUMAN":
                pipe.hincrby(key, "human_count", 1)
            elif result == "VOICEMAIL":
                pipe.hincrby(key, "voicemail_count", 1)
            elif result == "UNKNOWN":
                pipe.hincrby(key, "unknown_count", 1)
            if layer == 2:
                pipe.hincrby(key, "deepgram_calls", 1)
            pipe.expire(key, 172800)   # 2 días — margen de sobra para que usage_sync la drene
            await pipe.execute()
    except Exception as e:
        log.warning("Redis record_daily_usage error: %s", e)


# ── Stats de proveedor (mismo problema que daily_usage, más agudo) ───────────
# provider_stats tiene PRIMARY KEY (provider, key_idx, date) — solo ~15
# combinaciones posibles en total, compartidas entre TODOS los clientes (no
# una fila por cliente como daily_usage). Si la mayoría usa el mismo
# proveedor, todas las detecciones del sistema entero compiten por la MISMA
# fila. Mismo arreglo: contar en Redis, volcar a MySQL cada 15s.
def _provider_stat_key(provider: str, key_idx: int) -> str:
    return f"amd:pstat:{provider}:{key_idx}:{datetime.now().strftime('%Y-%m-%d')}"


async def record_provider_stat(provider: str, key_idx: int, audio_ms: int, error: bool = False) -> None:
    try:
        r = await get_redis()
        key = _provider_stat_key(provider, key_idx)
        async with r.pipeline(transaction=False) as pipe:
            pipe.hincrby(key, "requests", 1)
            if error:
                pipe.hincrby(key, "errors", 1)
            pipe.hincrby(key, "audio_ms", audio_ms)
            pipe.expire(key, 172800)
            await pipe.execute()
    except Exception as e:
        log.warning("Redis record_provider_stat error: %s", e)
