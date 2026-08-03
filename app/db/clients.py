from sqlalchemy import text
from app.db.engine import get_db


async def _invalidate_by_id(client_id: int) -> None:
    """Cache de auth cacheado por api_key (client_cache.get_client_cached) —
    cualquier función que modifique un cliente debe invalidarlo, si no el
    cambio (desactivar cliente, cambiar IPs permitidas, etc.) no se refleja
    hasta que expire el TTL (REDIS_CACHE_TTL, 300s)."""
    from app.cache.client_cache import invalidate_api_key
    async with get_db() as db:
        result = await db.execute(text("SELECT api_key FROM clients WHERE id=:id"), {"id": client_id})
        row = result.first()
    if row:
        await invalidate_api_key(row[0])


async def ensure_provider_column() -> None:
    """Migración: agrega columna provider si no existe (DBs anteriores al feature)."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE clients ADD COLUMN provider ENUM('groq','deepgram','deepgramv2') NOT NULL DEFAULT 'groq'"
            ))
    except Exception:
        pass  # ya existe

async def ensure_provider_deepgramv2() -> None:
    """Migración: amplía el ENUM de provider con todos los proveedores actuales."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE clients MODIFY COLUMN provider "
                "ENUM('groq','deepgram','deepgramv2','fireworks','together','openai','vosk','vosk_stream','sherpa') NOT NULL DEFAULT 'groq'"
            ))
    except Exception:
        pass


async def ensure_keywords_mode_column() -> None:
    """Migración: agrega columna keywords_mode (global/custom) si no existe."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE clients ADD COLUMN keywords_mode VARCHAR(10) NOT NULL DEFAULT 'global'"
            ))
    except Exception:
        pass


async def ensure_amd_mode_column() -> None:
    """Migración: agrega columna amd_mode (batch/stream) si no existe."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE clients ADD COLUMN amd_mode VARCHAR(10) NOT NULL DEFAULT 'batch'"
            ))
    except Exception:
        pass


async def set_keywords_mode(client_id: int, mode: str) -> None:
    """Fija explícitamente 'global' o 'custom' (selector en el panel)."""
    if mode not in ("global", "custom"):
        return
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET keywords_mode=:m WHERE id=:id"),
            {"m": mode, "id": client_id},
        )
    await _invalidate_by_id(client_id)


async def set_amd_mode(client_id: int, mode: str) -> None:
    """Fija explícitamente 'batch' o 'stream' (selector en el panel)."""
    if mode not in ("batch", "stream"):
        return
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET amd_mode=:m WHERE id=:id"),
            {"m": mode, "id": client_id},
        )
    await _invalidate_by_id(client_id)


async def ensure_amd_bias_column() -> None:
    """Migración: agrega columna amd_bias (conservative/aggressive) si no existe.

    'conservative' (default, comportamiento histórico): ante una transcripción
    ambigua en capa 2 (_classify_transcript), preferir VOICEMAIL — evita
    conectar al agente con un contestador real, a costa de perder algún lead.
    'aggressive': en esa misma ambigüedad, devolver UNKNOWN en vez de asumir
    VOICEMAIL — requiere que el dialplan del cliente enrute UNKNOWN al agente
    (transfer_agent), si no, no cambia nada en la práctica (ver
    agi/extensions_amd.conf, que hoy cuelga en UNKNOWN igual que en VOICEMAIL).
    """
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE clients ADD COLUMN amd_bias VARCHAR(12) NOT NULL DEFAULT 'conservative'"
            ))
    except Exception:
        pass  # ya existe


async def set_amd_bias(client_id: int, bias: str) -> None:
    """Fija explícitamente 'conservative' o 'aggressive' (selector en el panel)."""
    if bias not in ("conservative", "aggressive"):
        return
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET amd_bias=:b WHERE id=:id"),
            {"b": bias, "id": client_id},
        )
    await _invalidate_by_id(client_id)


async def get_client_by_apikey(api_key: str) -> dict | None:
    async with get_db() as db:
        result = await db.execute(
            text("""
                SELECT id, name, active, daily_limit, allowed_ips, provider, keywords_mode, amd_mode, amd_bias
                FROM clients WHERE api_key = :key LIMIT 1
            """),
            {"key": api_key},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def get_client_by_install_token(token: str) -> dict | None:
    async with get_db() as db:
        result = await db.execute(
            text("""
                SELECT id, name, active, api_key
                FROM clients WHERE install_token = :token LIMIT 1
            """),
            {"token": token},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def get_all_clients_with_stats() -> list[dict]:
    async with get_db() as db:
        result = await db.execute(text("""
            SELECT c.id, c.name, c.active, c.daily_limit, c.allowed_ips,
                   c.provider, c.keywords_mode, c.amd_mode, c.amd_bias, c.install_token, c.created_at,
                   COALESCE(u.total_calls, 0)     AS today_calls,
                   COALESCE(u.human_count, 0)     AS today_human,
                   COALESCE(u.voicemail_count, 0) AS today_voicemail
            FROM clients c
            LEFT JOIN daily_usage u ON u.client_id = c.id AND u.date = CURDATE()
            ORDER BY c.id
        """))
        rows = result.mappings().all()
    return [dict(r) for r in rows]


async def count_active_clients() -> int:
    async with get_db() as db:
        result = await db.execute(text("SELECT COUNT(*) FROM clients WHERE active=1"))
        return result.scalar() or 0


async def create_client(
    name: str, limit: int, api_key: str, install_token: str,
    provider: str = "groq", ips: str = "", notes: str = "",
) -> int:
    async with get_db() as db:
        result = await db.execute(
            text("""
                INSERT INTO clients
                    (name, api_key, install_token, active, daily_limit, provider, allowed_ips, notes)
                VALUES (:name, :key, :token, 1, :limit, :provider, :ips, :notes)
            """),
            {"name": name, "key": api_key, "token": install_token,
             "limit": limit, "provider": provider,
             "ips": ips or None, "notes": notes or None},
        )
        return result.lastrowid


async def update_client_limit(client_id: int, limit: int) -> None:
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET daily_limit=:l WHERE id=:id"),
            {"l": max(0, limit), "id": client_id},
        )
    await _invalidate_by_id(client_id)


async def update_client_name(client_id: int, name: str) -> None:
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET name=:name WHERE id=:id"),
            {"name": name.strip(), "id": client_id},
        )
    await _invalidate_by_id(client_id)


async def update_client_provider(client_id: int, provider: str) -> None:
    if provider not in ("groq", "deepgram", "deepgramv2", "fireworks", "together", "openai", "vosk", "vosk_stream", "sherpa"):
        return
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET provider=:p WHERE id=:id"),
            {"p": provider, "id": client_id},
        )
    await _invalidate_by_id(client_id)


async def toggle_client_active(client_id: int) -> bool:
    """Alterna active. Retorna el nuevo estado."""
    async with get_db() as db:
        result = await db.execute(
            text("SELECT active FROM clients WHERE id=:id"), {"id": client_id}
        )
        row = result.first()
        if not row:
            return False
        new_state = 0 if row[0] else 1
        await db.execute(
            text("UPDATE clients SET active=:s WHERE id=:id"),
            {"s": new_state, "id": client_id},
        )
    await _invalidate_by_id(client_id)
    return bool(new_state)


async def update_client_ips(client_id: int, ips: str) -> None:
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET allowed_ips=:ips WHERE id=:id"),
            {"ips": ips or None, "id": client_id},
        )
    await _invalidate_by_id(client_id)


async def rotate_api_key(client_id: int, new_key: str) -> str | None:
    """Cambia api_key. Retorna la key anterior (para invalidar cache)."""
    async with get_db() as db:
        result = await db.execute(
            text("SELECT api_key FROM clients WHERE id=:id"), {"id": client_id}
        )
        row = result.first()
        if not row:
            return None
        old_key = row[0]
        await db.execute(
            text("UPDATE clients SET api_key=:key WHERE id=:id"),
            {"key": new_key, "id": client_id},
        )
    return old_key


async def rotate_install_token(client_id: int, new_token: str) -> None:
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET install_token=:token WHERE id=:id"),
            {"token": new_token, "id": client_id},
        )


async def delete_client(client_id: int) -> None:
    """Elimina el cliente y todos sus datos (cascada manual)."""
    # Invalidar ANTES del DELETE — después ya no hay fila para leer el api_key.
    await _invalidate_by_id(client_id)
    async with get_db() as db:
        await db.execute(text("DELETE FROM client_keywords WHERE client_id = :id"), {"id": client_id})
        await db.execute(text("DELETE FROM daily_usage     WHERE client_id = :id"), {"id": client_id})
        await db.execute(text("DELETE FROM voxidet_logs        WHERE client_id = :id"), {"id": client_id})
        await db.execute(text("DELETE FROM clients         WHERE id = :id"),        {"id": client_id})


async def ping_db() -> bool:
    try:
        async with get_db() as db:
            await db.execute(text("SELECT 1"))
        return True
    except Exception as e:
        import logging
        logging.getLogger("voxidet.db").error("ping_db failed: %s", e)
        return False
