import json
from sqlalchemy import text
from app.db.engine import get_db

_DEFAULTS: dict[str, str] = {
    "groq":         "whisper-large-v3-turbo",
    "deepgram":     "nova-3-general",
    "deepgramv2":   "nova-3-general",
    "fireworks":    "whisper-v3-turbo",
    "together":     "openai/whisper-large-v3",
    "openai":       "gpt-4o-mini-transcribe",
    "vosk":         "vosk-model-es-0.42",
    "vosk_stream":  "vosk-model-es-0.42",
    "sherpa":       "whisper-large-v3-turbo",
    "sherpa_large": "whisper-large-v3",   # opt-in, v1.19.0 — ver CHANGELOG
}


async def ensure_provider_settings_table() -> None:
    async with get_db() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS provider_settings (
                provider   VARCHAR(20)  NOT NULL PRIMARY KEY,
                model      VARCHAR(100) NOT NULL DEFAULT '',
                key_models TEXT         DEFAULT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """))
        try:
            await db.execute(text(
                "ALTER TABLE provider_settings ADD COLUMN key_models TEXT DEFAULT NULL"
            ))
        except Exception:
            pass
        try:
            await db.execute(text(
                "ALTER TABLE provider_settings ADD COLUMN active TINYINT(1) NOT NULL DEFAULT 1"
            ))
        except Exception:
            pass
        # sherpa_large (v1.19.0): mucho más lento que el resto (whisper-large-v3
        # completo en CPU, ver CHANGELOG) — no debe aparecer "activo" y
        # seleccionable para clientes apenas alguien migre a esta versión.
        # INSERT IGNORE solo aplica esto la primera vez que se crea la fila —
        # si alguien lo activa después a mano, corridas futuras no lo pisan.
        _DEFAULT_INACTIVE = {"sherpa_large"}
        for p, m in _DEFAULTS.items():
            active_val = 0 if p in _DEFAULT_INACTIVE else 1
            await db.execute(
                text("INSERT IGNORE INTO provider_settings (provider, model, active) VALUES (:p, :m, :a)"),
                {"p": p, "m": m, "a": active_val},
            )
        # Configuración global VAD engine (no es un proveedor ASR, se guarda aquí por conveniencia)
        await db.execute(
            text("INSERT IGNORE INTO provider_settings (provider, model) VALUES ('__vad_engine__', 'stream')")
        )


async def get_all_provider_settings() -> list[dict]:
    async with get_db() as db:
        result = await db.execute(
            text("SELECT provider, model, key_models, active, updated_at FROM provider_settings ORDER BY provider")
        )
        rows = result.mappings().all()
    return [dict(r) for r in rows]


async def get_active_providers() -> list[str]:
    """Proveedores activos para mostrar en el selector de clientes."""
    async with get_db() as db:
        result = await db.execute(
            text("SELECT provider FROM provider_settings WHERE active=1 AND provider != '__vad_engine__'")
        )
        return [r[0] for r in result.fetchall()]


async def toggle_provider_active(provider: str) -> bool:
    """Invierte el estado activo. Devuelve el nuevo valor."""
    async with get_db() as db:
        result = await db.execute(
            text("SELECT active FROM provider_settings WHERE provider=:p"),
            {"p": provider},
        )
        row = result.first()
        new_val = 0 if (row and row[0]) else 1
        await db.execute(
            text("UPDATE provider_settings SET active=:v WHERE provider=:p"),
            {"v": new_val, "p": provider},
        )
    return bool(new_val)


async def get_provider_key_models_db(provider: str) -> tuple[str, dict[str, str]]:
    """Devuelve (modelo_global, {idx_str: modelo})."""
    async with get_db() as db:
        result = await db.execute(
            text("SELECT model, key_models FROM provider_settings WHERE provider=:p"),
            {"p": provider},
        )
        row = result.first()
    if not row:
        return _DEFAULTS.get(provider, ""), {}
    global_model = row[0] or _DEFAULTS.get(provider, "")
    key_models   = json.loads(row[1]) if row[1] else {}
    return global_model, key_models


async def update_provider_models(
    provider: str,
    global_model: str,
    key_models: dict[str, str],
) -> None:
    async with get_db() as db:
        await db.execute(
            text("UPDATE provider_settings SET model=:m, key_models=:km WHERE provider=:p"),
            {"m": global_model, "km": json.dumps(key_models) if key_models else None, "p": provider},
        )


# ── Cache Redis ────────────────────────────────────────────────────────────────

async def _get_redis():
    from app.cache.client_cache import get_redis
    return await get_redis()


async def _cache_set(key: str, value: str) -> None:
    try:
        r = await _get_redis()
        await r.set(key, value)
    except Exception:
        pass


async def _cache_get(key: str) -> str | None:
    try:
        r = await _get_redis()
        val = await r.get(key)
        return val.decode() if val else None
    except Exception:
        return None


async def load_all_models_to_cache() -> None:
    """Startup: carga modelos globales y por-key a Redis."""
    rows = await get_all_provider_settings()
    for row in rows:
        provider = row["provider"]
        await _cache_set(f"provider:model:{provider}", row["model"] or _DEFAULTS.get(provider, ""))
        if row.get("key_models"):
            km = json.loads(row["key_models"])
            for idx, model in km.items():
                await _cache_set(f"provider:keymodel:{provider}:{idx}", model)


async def cache_provider_models(
    provider: str,
    global_model: str,
    key_models: dict[str, str],
) -> None:
    await _cache_set(f"provider:model:{provider}", global_model)
    for idx, model in key_models.items():
        await _cache_set(f"provider:keymodel:{provider}:{idx}", model)


_VAD_REDIS_KEY = "settings:vad_engine"


async def get_vad_engine() -> str:
    """Redis (caché) → MySQL. El valor persiste aunque Redis se reinicie."""
    val = await _cache_get(_VAD_REDIS_KEY)
    if val:
        return val
    async with get_db() as db:
        result = await db.execute(
            text("SELECT model FROM provider_settings WHERE provider='__vad_engine__'")
        )
        row = result.first()
    engine = row[0] if row else "stream"
    await _cache_set(_VAD_REDIS_KEY, engine)
    return engine


async def set_vad_engine(engine: str) -> None:
    """Guarda en MySQL (persistente) y actualiza caché Redis."""
    async with get_db() as db:
        await db.execute(
            text("UPDATE provider_settings SET model=:e WHERE provider='__vad_engine__'"),
            {"e": engine},
        )
    await _cache_set(_VAD_REDIS_KEY, engine)


async def get_provider_model(provider: str, key_idx: int | None = None) -> str:
    """Obtiene el modelo para un proveedor, opcionalmente por índice de key.
    Redis → DB → default. Sin esperas en el camino caliente."""
    if key_idx is not None:
        val = await _cache_get(f"provider:keymodel:{provider}:{key_idx}")
        if val:
            return val
        # key_idx >= KEY_ID_OFFSET → key gestionada desde el panel (provider_keys,
        # v1.16.0), no una key legacy de .env — el modelo vive en esa tabla, no
        # en el JSON key_models de provider_settings.
        from app.db.provider_keys import KEY_ID_OFFSET, get_key_model
        if key_idx >= KEY_ID_OFFSET:
            val = await get_key_model(key_idx - KEY_ID_OFFSET)
            if val:
                return val
    val = await _cache_get(f"provider:model:{provider}")
    if val:
        return val
    global_model, _ = await get_provider_key_models_db(provider)
    return global_model
