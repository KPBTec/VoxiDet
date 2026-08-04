"""
db/provider_keys.py — API keys de proveedores ASR gestionadas desde el panel
admin (v1.16.0, ver CHANGELOG), cifradas en reposo (core/secrets_crypto.py).

Conviven con las keys legacy de credentials.conf (GROQ_API_KEY_1, _2, ...) —
no las reemplazan. get_active_keys() combina ambas fuentes en una sola lista
para la rotación real (amd_engine.py).

Namespacing de IDs: las keys legacy de .env mantienen su índice posicional
de siempre (0, 1, 2...) — cero cambios para instalaciones existentes, sigue
funcionando el modelo-por-key que ya tenían cacheado. Las keys nuevas
guardadas en esta tabla usan id = KEY_ID_OFFSET + id_real_de_fila, para que
nunca choquen con los índices legacy (nadie va a tener 100.000 keys).
"""
import json

from sqlalchemy import text
from app.db.engine import get_db
from app.config import settings
from app.core.secrets_crypto import encrypt_key, decrypt_key, mask_key

KEY_ID_OFFSET = 100_000

_ACTIVE_KEYS_CACHE_PREFIX = "amd:active_keys:"


async def _invalidate_active_keys_cache(provider: str) -> None:
    """get_active_keys() se llama hasta 5x por detección (una por proveedor
    intentado en el fallback) sin caché — cachear evita pegarle a MySQL +
    desencriptar Fernet en cada request, pero necesita invalidarse en
    cualquier mutación que cambie qué keys están activas."""
    try:
        from app.cache.client_cache import get_redis
        r = await get_redis()
        await r.delete(f"{_ACTIVE_KEYS_CACHE_PREFIX}{provider}")
    except Exception:
        pass

_ENV_GETTERS = {
    "groq":      lambda: settings.get_groq_keys(),
    "deepgram":  lambda: settings.get_deepgram_keys(),
    "fireworks": lambda: settings.get_fireworks_keys(),
    "together":  lambda: settings.get_together_keys(),
    "openai":    lambda: settings.get_openai_keys(),
}


async def ensure_provider_keys_table() -> None:
    async with get_db() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS provider_keys (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                provider      VARCHAR(20)  NOT NULL,
                key_encrypted TEXT         NOT NULL,
                key_masked    VARCHAR(20)  NOT NULL,
                model         VARCHAR(100) NOT NULL DEFAULT '',
                active        TINYINT(1)   NOT NULL DEFAULT 1,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_provider (provider)
            )
        """))
        # Keys legacy de .env desactivadas desde el panel (v1.16.1) — el valor
        # real sigue en credentials.conf (no se puede reescribir el archivo
        # desde acá), esta tabla solo dice "no la uses para rotar". La sola
        # presencia de la fila significa desactivada; sin fila = activa.
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS provider_legacy_disabled (
                provider   VARCHAR(20) NOT NULL,
                key_index  INT         NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (provider, key_index)
            )
        """))


async def add_key(provider: str, plaintext: str, model: str = "") -> int:
    plaintext = plaintext.strip()
    async with get_db() as db:
        result = await db.execute(
            text("""INSERT INTO provider_keys (provider, key_encrypted, key_masked, model)
                     VALUES (:p, :ke, :km, :m)"""),
            {"p": provider, "ke": encrypt_key(plaintext), "km": mask_key(plaintext), "m": model},
        )
        new_id = result.lastrowid
    await _invalidate_active_keys_cache(provider)
    return new_id


async def list_db_keys(provider: str) -> list[dict]:
    """Filas de DB para la UI del panel — nunca incluye el valor descifrado."""
    async with get_db() as db:
        result = await db.execute(
            text("""SELECT id, key_masked, model, active, updated_at
                     FROM provider_keys WHERE provider=:p ORDER BY id"""),
            {"p": provider},
        )
        rows = [dict(r) for r in result.mappings().all()]
    for r in rows:
        r["display_id"] = KEY_ID_OFFSET + r["id"]  # id que usa la rotación/caché
    return rows


async def set_key_active(key_id: int, active: bool) -> None:
    async with get_db() as db:
        result = await db.execute(
            text("SELECT provider FROM provider_keys WHERE id=:id"), {"id": key_id}
        )
        row = result.first()
        await db.execute(
            text("UPDATE provider_keys SET active=:a WHERE id=:id"),
            {"a": 1 if active else 0, "id": key_id},
        )
    if row:
        await _invalidate_active_keys_cache(row[0])


async def set_key_model(provider: str, key_id: int, model: str) -> None:
    async with get_db() as db:
        await db.execute(
            text("UPDATE provider_keys SET model=:m WHERE id=:id"),
            {"m": model, "id": key_id},
        )
    # Cachear en Redis bajo el id-con-offset, mismo camino rápido que usa
    # get_provider_model() para las keys legacy — evita ir a MySQL en el
    # camino caliente de cada detección.
    from app.db.providers import _cache_set
    await _cache_set(f"provider:keymodel:{provider}:{KEY_ID_OFFSET + key_id}", model)


async def delete_key(key_id: int) -> None:
    async with get_db() as db:
        result = await db.execute(
            text("SELECT provider FROM provider_keys WHERE id=:id"), {"id": key_id}
        )
        row = result.first()
        await db.execute(text("DELETE FROM provider_keys WHERE id=:id"), {"id": key_id})
    if row:
        await _invalidate_active_keys_cache(row[0])


async def get_disabled_legacy_indices(provider: str) -> set[int]:
    async with get_db() as db:
        result = await db.execute(
            text("SELECT key_index FROM provider_legacy_disabled WHERE provider=:p"),
            {"p": provider},
        )
        return {r[0] for r in result.fetchall()}


async def set_legacy_key_active(provider: str, key_index: int, active: bool) -> None:
    """No reescribe credentials.conf (no se puede desde acá) — solo marca la
    key para que la rotación real (get_active_keys) la salte o no."""
    async with get_db() as db:
        if active:
            await db.execute(
                text("DELETE FROM provider_legacy_disabled WHERE provider=:p AND key_index=:i"),
                {"p": provider, "i": key_index},
            )
        else:
            await db.execute(
                text("INSERT IGNORE INTO provider_legacy_disabled (provider, key_index) VALUES (:p, :i)"),
                {"p": provider, "i": key_index},
            )
    await _invalidate_active_keys_cache(provider)


async def get_key_model(db_id: int) -> str:
    """db_id sin offset — usado por get_provider_model() como fallback si no
    está en caché."""
    async with get_db() as db:
        result = await db.execute(text("SELECT model FROM provider_keys WHERE id=:id"), {"id": db_id})
        row = result.first()
    return row[0] if row and row[0] else ""


async def get_active_keys(provider: str) -> list[dict]:
    """Fuente única para la rotación real (amd_engine.py, stream.py):
    legacy .env (id = índice 0,1,2..., salteando las desactivadas desde el
    panel — v1.16.1, provider_legacy_disabled) + DB (id = KEY_ID_OFFSET + fila,
    solo active=1, descifradas en memoria acá mismo).
    Devuelve [{id, key}] — 'key' es texto plano: usar solo en memoria, jamás
    loguearlo ni devolverlo en una respuesta HTTP.

    Cacheado en Redis (ya desencriptado — mismo modelo de confianza que
    get_client_cached(), que también cachea el registro completo del
    cliente) porque se llama hasta 5x por detección en batch (una vez por
    proveedor intentado en el fallback) y 2-3x por sesión en stream, sin
    ningún cache antes — pegaba a MySQL + Fernet en cada intento."""
    cache_key = f"{_ACTIVE_KEYS_CACHE_PREFIX}{provider}"
    try:
        from app.cache.client_cache import get_redis
        r = await get_redis()
        cached = await r.get(cache_key)
        if cached is not None:
            return json.loads(cached)
    except Exception:
        pass

    out: list[dict] = []

    getter = _ENV_GETTERS.get(provider)
    if getter:
        disabled = await get_disabled_legacy_indices(provider)
        for i, k in enumerate(getter()):
            if i not in disabled:
                out.append({"id": i, "key": k})

    async with get_db() as db:
        result = await db.execute(
            text("SELECT id, key_encrypted FROM provider_keys WHERE provider=:p AND active=1 ORDER BY id"),
            {"p": provider},
        )
        rows = result.mappings().all()
    for row in rows:
        plain = decrypt_key(row["key_encrypted"])
        if plain:
            out.append({"id": KEY_ID_OFFSET + row["id"], "key": plain})

    try:
        from app.cache.client_cache import get_redis
        r = await get_redis()
        await r.setex(cache_key, settings.REDIS_CACHE_TTL, json.dumps(out))
    except Exception:
        pass

    return out


async def get_all_keys_for_admin(provider: str) -> list[dict]:
    """Para el panel (a diferencia de get_active_keys, incluye también las
    desactivadas — si no, no habría forma de volver a activarlas desde ahí).
    [{id, key, source, active}] — 'key' en texto plano, solo para armar el
    enmascarado/consultar modelos disponibles, nunca se devuelve tal cual."""
    out: list[dict] = []

    getter = _ENV_GETTERS.get(provider)
    if getter:
        disabled = await get_disabled_legacy_indices(provider)
        for i, k in enumerate(getter()):
            out.append({"id": i, "key": k, "source": "env", "active": i not in disabled})

    async with get_db() as db:
        result = await db.execute(
            text("SELECT id, key_encrypted, active FROM provider_keys WHERE provider=:p ORDER BY id"),
            {"p": provider},
        )
        rows = result.mappings().all()
    for row in rows:
        plain = decrypt_key(row["key_encrypted"])
        if plain:
            out.append({
                "id": KEY_ID_OFFSET + row["id"], "key": plain,
                "source": "db", "active": bool(row["active"]),
            })

    return out
