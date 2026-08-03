from sqlalchemy import text
from app.db.engine import get_db
from app.cache.client_cache import record_daily_usage


async def ensure_log_provider_column() -> None:
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE voxidet_logs ADD COLUMN provider VARCHAR(15) DEFAULT NULL AFTER layer_used"
            ))
    except Exception:
        # Columna ya existe — ampliar si sigue siendo VARCHAR(10)
        try:
            async with get_db() as db:
                await db.execute(text(
                    "ALTER TABLE voxidet_logs MODIFY COLUMN provider VARCHAR(15) DEFAULT NULL"
                ))
        except Exception:
            pass


async def ensure_result_enum_error() -> None:
    """Migración: agrega 'ERROR' al ENUM de result — stream.py y el AGI batch
    ya escriben ese valor cuando falla la detección, pero el ENUM original
    no lo incluía y el INSERT truncaba con error 1265."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE voxidet_logs MODIFY COLUMN result "
                "ENUM('HUMAN','VOICEMAIL','UNKNOWN','ERROR') NOT NULL"
            ))
    except Exception:
        pass


async def ensure_created_at_index() -> None:
    """Migración: índice dedicado en created_at — los reportes/dashboards sin
    filtro de cliente no pueden usar idx_client_date (client_id va primero)."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE voxidet_logs ADD INDEX idx_created_at (created_at)"
            ))
    except Exception:
        pass


async def ensure_caller_id_index() -> None:
    """Migración: índice en caller_id — la búsqueda por teléfono en el panel
    admin usa prefijo (LIKE 'numero%'), que SÍ puede usar este índice (a
    diferencia de un LIKE '%numero%' con comodín al inicio, que ningún
    índice acelera)."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE voxidet_logs ADD INDEX idx_caller_id (caller_id)"
            ))
    except Exception:
        pass


async def ensure_beep_detected_column() -> None:
    """Migración: columna para guardar si se detectó tono de beep (experimental,
    ver app/core/tone_detector.py) — antes solo quedaba en logs de texto, sin
    forma de comparar contra el resultado real desde el panel."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE voxidet_logs ADD COLUMN beep_detected TINYINT(1) NOT NULL DEFAULT 0"
            ))
    except Exception:
        pass


async def ensure_mode_transcript_columns() -> None:
    """Migración: antes el transcript vivía metido en param3 (VARCHAR(100),
    truncando en silencio cualquier cosa más larga — el código ya asumía
    hasta 500 chars) y no había forma de saber si una fila vino de modo
    batch o stream sin inferirlo de combinaciones de otros campos. Columnas
    dedicadas, aditivas — param1-4 quedan intactos para lo que ya usaban
    (lead_id/campaign_id/list_id en batch, session_id en stream)."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE voxidet_logs ADD COLUMN mode ENUM('batch','stream') "
                "NOT NULL DEFAULT 'batch' AFTER layer_used"
            ))
    except Exception:
        pass
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE voxidet_logs ADD COLUMN transcript VARCHAR(500) DEFAULT NULL"
            ))
    except Exception:
        pass


async def ensure_layer2_calls_column() -> None:
    """Migración: daily_usage.deepgram_calls quedó con nombre de un solo
    proveedor (era la única capa 2 cuando se creó la tabla) — ahora hay 5
    proveedores posibles en capa 2 (groq/openai/together/fireworks/deepgram).
    Columna nueva con nombre genérico; deepgram_calls queda sin usar en vez
    de renombrarse, para no romper ningún reporte externo que ya la lea."""
    try:
        async with get_db() as db:
            await db.execute(text(
                "ALTER TABLE daily_usage ADD COLUMN layer2_calls INT DEFAULT 0 AFTER deepgram_calls"
            ))
    except Exception:
        pass


async def save_log(
    client_id: int,
    call_id: str,
    caller_id: str,
    result: str,
    layer: int,
    latency_ms: int,
    audio_secs: float,
    mode: str = "batch",
    provider: str = "",
    transcript: str = "",
    param1: str = "",
    param2: str = "",
    param3: str = "",
    param4: str = "",
    beep_detected: bool = False,
) -> None:
    async with get_db() as db:
        await db.execute(
            text("""
                INSERT INTO voxidet_logs
                    (client_id, call_id, caller_id, result, layer_used, mode, provider,
                     latency_ms, audio_secs, transcript, param1, param2, param3, param4, beep_detected)
                VALUES
                    (:cid, :call_id, :caller_id, :result, :layer, :mode, :provider,
                     :lat, :secs, :transcript, :p1, :p2, :p3, :p4, :beep)
            """),
            {
                "cid": client_id, "call_id": call_id, "caller_id": caller_id,
                "result": result, "layer": layer, "mode": mode, "provider": provider or None,
                "lat": latency_ms, "secs": audio_secs,
                "transcript": (transcript or "")[:500] or None,
                "p1": (param1 or "")[:200] or None,
                "p2": (param2 or "")[:200] or None,
                "p3": (param3 or "")[:100] or None,
                "p4": (param4 or "")[:200] or None,
                "beep": 1 if beep_detected else 0,
            },
        )
    # Contador de uso diario: Redis (sin contención de locks), NO MySQL
    # directo — ver client_cache.record_daily_usage y app/core/usage_sync.py
    await record_daily_usage(client_id, result, layer)


async def get_logs_since(
    last_id: int,
    limit: int = 200,
    caller_filter: str | None = None,
    result_filter: str | None = None,
    param1_filter: str | None = None,
    session_filter: str | None = None,
    date_filter: str | None = None,
) -> list[dict]:
    """Retorna logs con id > last_id. Usado por el stream SSE del CMS."""
    where = "WHERE l.id > :last_id"
    params: dict = {"last_id": last_id, "limit": limit}

    if caller_filter:
        # Prefijo (LIKE 'numero%'), no substring — así sí puede usar idx_caller_id.
        where += " AND l.caller_id LIKE :caller"
        params["caller"] = f"{caller_filter}%"
    if result_filter:
        where += " AND l.result = :result"
        params["result"] = result_filter
    if param1_filter:
        where += " AND l.param1 = :p1"
        params["p1"] = param1_filter
    if session_filter:
        where += " AND l.param2 = :p2"
        params["p2"] = session_filter
    if date_filter:
        # Rango, no DATE(l.created_at) = :d — envolver la COLUMNA en una
        # función impide usar cualquier índice sobre created_at. DATE_ADD
        # aquí envuelve el parámetro, no la columna, así que no afecta el uso
        # del índice.
        where += " AND l.created_at >= :d0 AND l.created_at < DATE_ADD(:d0, INTERVAL 1 DAY)"
        params["d0"] = f"{date_filter} 00:00:00"

    async with get_db() as db:
        result = await db.execute(
            text(f"""
                SELECT l.id, TIME(l.created_at) AS time, l.created_at AS datetime,
                       c.name AS client, l.caller_id, l.result,
                       l.layer_used AS layer, l.mode, l.provider, l.latency_ms,
                       l.transcript, l.param1, l.param2, l.param3, l.param4, l.beep_detected
                FROM voxidet_logs l
                JOIN clients c ON c.id = l.client_id
                {where}
                ORDER BY l.id ASC
                LIMIT :limit
            """),
            params,
        )
        rows = result.mappings().all()
    return [dict(r) for r in rows]


async def get_recent_logs(
    client_id: int,
    limit: int = 50,
    caller_filter: str | None = None,
    result_filter: str | None = None,
    param1_filter: str | None = None,
    session_filter: str | None = None,
    date_filter: str | None = None,
) -> list[dict]:
    # client_id=0 → vista admin global (todos los clientes)
    where = "WHERE 1=1" if client_id == 0 else "WHERE l.client_id = :cid"
    params: dict = {"limit": limit}
    if client_id != 0:
        params["cid"] = client_id
    if caller_filter:
        # Prefijo (LIKE 'numero%'), no substring — así sí puede usar idx_caller_id.
        where += " AND l.caller_id LIKE :caller"
        params["caller"] = f"{caller_filter}%"
    if result_filter:
        where += " AND l.result = :result"
        params["result"] = result_filter
    if param1_filter:
        where += " AND l.param1 = :p1"
        params["p1"] = param1_filter
    if session_filter:
        where += " AND l.param2 = :p2"
        params["p2"] = session_filter
    if date_filter:
        where += " AND l.created_at >= :d0 AND l.created_at < DATE_ADD(:d0, INTERVAL 1 DAY)"
        params["d0"] = f"{date_filter} 00:00:00"

    async with get_db() as db:
        result = await db.execute(
            text(f"""
                SELECT
                    l.id,
                    TIME(l.created_at)  AS time,
                    l.created_at        AS datetime,
                    c.name              AS client,
                    l.caller_id,
                    l.call_id,
                    l.result,
                    l.layer_used        AS layer,
                    l.mode,
                    l.provider,
                    l.latency_ms,
                    l.audio_secs,
                    l.transcript,
                    l.param1, l.param2, l.param3, l.param4, l.beep_detected
                FROM voxidet_logs l
                JOIN clients c ON c.id = l.client_id
                {where}
                ORDER BY l.id DESC
                LIMIT :limit
            """),
            params,
        )
        rows = result.mappings().all()
    return [dict(r) for r in rows]
