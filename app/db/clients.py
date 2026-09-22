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


async def ensure_amd_mode_audiosocket_width() -> None:
    """Migración (v1.27.0): 'audiosocket' son 11 caracteres — no entra en el
    VARCHAR(10) original de amd_mode (ensure_amd_mode_column), MySQL lo
    trunca en silencio a 'audiosocke' y set_amd_mode() nunca lo iba a
    reconocer de vuelta. Ancho con margen para no repetir esto si sale un
    modo nuevo más largo."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE clients MODIFY COLUMN amd_mode VARCHAR(20) NOT NULL DEFAULT 'batch'"
            ))
    except Exception:
        pass


async def set_amd_mode(client_id: int, mode: str) -> None:
    """Fija explícitamente 'batch', 'stream' o 'audiosocket' (selector en el panel)."""
    if mode not in ("batch", "stream", "audiosocket"):
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


async def ensure_detection_mode_column() -> None:
    """Migración: agrega detection_mode (energia_primero/transcripcion_directa).

    'energia_primero' (default, comportamiento histórico): capa 1 (energía,
    sin transcribir) decide primero; si es inconclusa, recién ahí cae a capa 2
    (transcripción real). 'transcripcion_directa': se salta la capa 1 siempre
    y decide leyendo el texto transcripto desde el primer momento — pedido
    explícito para clientes donde la heurística de energía por sí sola no da
    suficiente confianza y prefieren pagar el costo de transcribir siempre.
    """
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE clients ADD COLUMN detection_mode VARCHAR(32) NOT NULL DEFAULT 'energia_primero'"
            ))
    except Exception:
        pass  # ya existe


async def ensure_detection_mode_width() -> None:
    """Migración de emergencia (mismo día que ensure_detection_mode_column):
    'transcripcion_directa' son 21 caracteres — no entraba en el VARCHAR(20)
    original, y a diferencia de amd_mode (que MySQL truncó en silencio, ver
    ensure_amd_mode_audiosocket_width) acá el modo estricto de MySQL en este
    servidor lo rechazó de frente con error 1406 al primer clic real en el
    panel ("Data too long for column 'detection_mode'"). Ensanchado a 32 con
    margen real esta vez."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE clients MODIFY COLUMN detection_mode VARCHAR(32) NOT NULL DEFAULT 'energia_primero'"
            ))
    except Exception:
        pass


async def set_detection_mode(client_id: int, mode: str) -> None:
    if mode not in ("energia_primero", "transcripcion_directa"):
        return
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET detection_mode=:m WHERE id=:id"),
            {"m": mode, "id": client_id},
        )
    await _invalidate_by_id(client_id)


async def ensure_record_seconds_column() -> None:
    """Migración: agrega record_seconds (2-6, default 3 = comportamiento
    histórico hardcodeado que tenía el AGI). Controla cuánto graba Asterisk
    ANTES de mandar el audio al servidor — ver /amd/check y agi_template.py."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE clients ADD COLUMN record_seconds TINYINT NOT NULL DEFAULT 3"
            ))
    except Exception:
        pass  # ya existe


async def set_record_seconds(client_id: int, seconds: int) -> None:
    if seconds not in (2, 3, 4, 5, 6):
        return
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET record_seconds=:s WHERE id=:id"),
            {"s": seconds, "id": client_id},
        )
    await _invalidate_by_id(client_id)


async def ensure_fallback_enabled_column() -> None:
    """Migración: agrega fallback_enabled (default 1 = comportamiento
    histórico). En 0, si el proveedor elegido del cliente falla/no reconoce
    nada, el resultado queda UNKNOWN en vez de probar otros proveedores —
    pedido explícito para evitar que Sherpa (u otro) "invente" texto en
    llamadas donde el proveedor principal no encontró nada."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE clients ADD COLUMN fallback_enabled TINYINT(1) NOT NULL DEFAULT 1"
            ))
    except Exception:
        pass  # ya existe


async def set_fallback_enabled(client_id: int, enabled: bool) -> None:
    async with get_db() as db:
        await db.execute(
            text("UPDATE clients SET fallback_enabled=:e WHERE id=:id"),
            {"e": 1 if enabled else 0, "id": client_id},
        )
    await _invalidate_by_id(client_id)


async def get_client_by_apikey(api_key: str) -> dict | None:
    async with get_db() as db:
        result = await db.execute(
            text("""
                SELECT id, name, active, daily_limit, allowed_ips, provider, keywords_mode, amd_mode, amd_bias,
                       detection_mode, record_seconds, fallback_enabled
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


async def get_all_clients_with_stats(site_id: int | None = None) -> list[dict]:
    async with get_db() as db:
        result = await db.execute(text("""
            SELECT c.id, c.name, c.active, c.daily_limit, c.allowed_ips,
                   c.provider, c.keywords_mode, c.amd_mode, c.amd_bias, c.install_token, c.created_at,
                   c.detection_mode, c.record_seconds, c.fallback_enabled,
                   c.site_id, s.name AS site_name,
                   COALESCE(u.total_calls, 0)     AS today_calls,
                   COALESCE(u.human_count, 0)     AS today_human,
                   COALESCE(u.voicemail_count, 0) AS today_voicemail
            FROM clients c
            LEFT JOIN daily_usage u ON u.client_id = c.id AND u.date = CURDATE()
            LEFT JOIN sites s ON s.id = c.site_id
            ORDER BY c.id
        """))
        rows = [dict(r) for r in result.mappings().all()]
    if site_id is not None:
        rows = [r for r in rows if r["site_id"] == site_id]
    return rows


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
