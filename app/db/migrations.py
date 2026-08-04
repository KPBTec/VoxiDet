"""
Migraciones de schema versionadas — reemplaza el patrón viejo de 20 funciones
`ensure_*` sueltas, llamadas a mano y en orden fijo en `lifespan()`
(app/main.py), cada una reintentando su propio ALTER/CREATE en cada arranque
y tragándose el error de "ya existe" con un `except: pass`. Mismo mecanismo
que adoptó KPBTec_voxikam (tabla `schema_migrations` + array versionado,
`deploy.sh`) para dejar de tener drift entre lo que el código asume aplicado
y lo que realmente hay en la DB.

Nota sobre los números de versión: para las migraciones que ya existían se
usó la versión real de CHANGELOG.md donde se pudo confirmar cuándo se
introdujo cada cambio. Las que no tienen una entrada de CHANGELOG rastreable
por grep quedan agrupadas bajo "1.0.0" (baseline) — no es necesariamente el
release exacto en que se agregaron, pero como son features fundacionales ya
aplicadas en cualquier instalación real existente, el número exacto no
afecta el comportamiento, solo el identificador único en `schema_migrations`.

Migraciones nuevas se agregan SOLO acá (agregar una tupla al final de
MIGRATIONS con la función a ejecutar), nunca como función `ensure_*` suelta
llamada directo desde `lifespan()` otra vez.
"""
import logging

from sqlalchemy import text

from app.db.engine import get_db
from app.db.clients import (
    ensure_provider_column,
    ensure_provider_deepgramv2,
    ensure_keywords_mode_column,
    ensure_amd_mode_column,
    ensure_amd_bias_column,
)
from app.db.admin_users import ensure_admin_users_table
from app.db.firewall import ensure_firewall_table, ensure_fail2ban_unban_table
from app.db.providers import ensure_provider_settings_table
from app.db.provider_keys import ensure_provider_keys_table
from app.db.logs import (
    ensure_log_provider_column,
    ensure_result_enum_error,
    ensure_created_at_index,
    ensure_caller_id_index,
    ensure_beep_detected_column,
    ensure_mode_transcript_columns,
    ensure_layer2_calls_column,
)
from app.db.keywords import ensure_table as ensure_keywords_table
from app.db.client_keywords import ensure_client_keywords_table
from app.db.provider_stats import ensure_provider_stats_table
from app.db.settings import ensure_app_settings_table
from app.db.audit import ensure_audit_log_table

log = logging.getLogger("voxidet.migrations")


async def _run_all(*fns) -> None:
    for fn in fns:
        await fn()


# (versión, función a ejecutar) — se aplican en este orden, una sola vez cada una.
MIGRATIONS = [
    ("1.0.0", lambda: _run_all(
        ensure_provider_column,
        ensure_provider_deepgramv2,
        ensure_keywords_mode_column,
        ensure_amd_mode_column,
        ensure_admin_users_table,
        ensure_firewall_table,
        ensure_provider_settings_table,
        ensure_keywords_table,
        ensure_client_keywords_table,
        ensure_provider_stats_table,
    )),
    ("1.1.0", ensure_log_provider_column),
    ("1.5.0", lambda: _run_all(ensure_result_enum_error, ensure_created_at_index)),
    ("1.5.1", ensure_fail2ban_unban_table),
    ("1.7.2", ensure_caller_id_index),
    ("1.8.2", ensure_beep_detected_column),
    ("1.12.0", lambda: _run_all(ensure_mode_transcript_columns, ensure_layer2_calls_column)),
    ("1.13.0", ensure_amd_bias_column),
    ("1.16.0", ensure_provider_keys_table),
    ("1.22.0", ensure_app_settings_table),
    ("1.23.0", ensure_audit_log_table),
]


async def _ensure_schema_migrations_table() -> None:
    async with get_db() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    VARCHAR(20) PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))


async def run_pending_migrations() -> None:
    await _ensure_schema_migrations_table()
    async with get_db() as db:
        result = await db.execute(text("SELECT version FROM schema_migrations"))
        applied = {row[0] for row in result.all()}

    for version, fn in MIGRATIONS:
        if version in applied:
            continue
        log.info("Aplicando migración %s...", version)
        await fn()
        async with get_db() as db:
            await db.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": version},
            )
        log.info("Migración %s aplicada.", version)
