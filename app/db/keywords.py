from sqlalchemy import text
from app.db.engine import get_db

_CREATE = """
CREATE TABLE IF NOT EXISTS voxidet_keywords (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    word       VARCHAR(100) NOT NULL,
    type       ENUM('HUMAN','VOICEMAIL') NOT NULL,
    active     TINYINT(1)  NOT NULL DEFAULT 1,
    created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_word_type (word, type)
)
"""

_SEED = [
    ("aló",          "HUMAN"),    ("alo",          "HUMAN"),
    ("hola",         "HUMAN"),    ("bueno",         "HUMAN"),
    ("diga",         "HUMAN"),    ("dígame",        "HUMAN"),
    ("sí",           "HUMAN"),    ("si",            "HUMAN"),
    ("yes",          "HUMAN"),    ("hello",         "HUMAN"),
    ("habla",        "HUMAN"),    ("buenos",        "HUMAN"),
    ("buenas",       "HUMAN"),    ("espera",        "HUMAN"),
    ("buzón",        "VOICEMAIL"),("buzon",         "VOICEMAIL"),
    ("mensaje",      "VOICEMAIL"),("disponible",    "VOICEMAIL"),
    ("momento",      "VOICEMAIL"),("comuníquese",   "VOICEMAIL"),
    ("comuniquese",  "VOICEMAIL"),("después",       "VOICEMAIL"),
    ("despues",      "VOICEMAIL"),("marque",        "VOICEMAIL"),
    ("deje",         "VOICEMAIL"),("grabación",     "VOICEMAIL"),
    ("grabacion",    "VOICEMAIL"),("bip",           "VOICEMAIL"),
    ("beep",         "VOICEMAIL"),("tono",          "VOICEMAIL"),
    ("llamada",      "VOICEMAIL"),("comunicar",     "VOICEMAIL"),
    ("atender",      "VOICEMAIL"),("operador",      "VOICEMAIL"),
    ("gracias",      "VOICEMAIL"),("bienvenido",    "VOICEMAIL"),
    ("bienvenida",   "VOICEMAIL"),("intentelo",     "VOICEMAIL"),
    ("intento",      "VOICEMAIL"),
    ("asistente",    "VOICEMAIL"),("llamadas",      "VOICEMAIL"),
    ("servicio",     "VOICEMAIL"),("automatico",    "VOICEMAIL"),
    ("automático",   "VOICEMAIL"),("comunicado",    "VOICEMAIL"),
    ("número",       "VOICEMAIL"),("numero",        "VOICEMAIL"),
    ("extensión",    "VOICEMAIL"),("extension",     "VOICEMAIL"),
    ("forwarded",    "VOICEMAIL"),("voicemail",     "VOICEMAIL"),
]


async def ensure_table() -> None:
    try:
        # Migración: instalaciones viejas (pre-rename) tienen la tabla como
        # amd_keywords — renombrarla preserva las keywords ya configuradas
        # en vez de crear voxidet_keywords vacía al lado.
        async with get_db() as db:
            await db.execute(text("RENAME TABLE amd_keywords TO voxidet_keywords"))
    except Exception:
        pass  # ya no existe amd_keywords (instalación nueva o ya migrada)

    try:
        async with get_db() as db:
            await db.execute(text(_CREATE))

        # Seed en un solo INSERT multi-row — menos contención entre workers
        placeholders = ", ".join(f"(:w{i}, :t{i})" for i in range(len(_SEED)))
        params = {f"w{i}": w for i, (w, _) in enumerate(_SEED)}
        params.update({f"t{i}": t for i, (_, t) in enumerate(_SEED)})
        async with get_db() as db:
            await db.execute(
                text(f"INSERT IGNORE INTO voxidet_keywords (word, type) VALUES {placeholders}"),
                params,
            )
    except Exception:
        # Otro worker ya lo hizo — no es error crítico
        pass


def _kw_sort_key(kw: dict) -> tuple:
    import unicodedata
    w = unicodedata.normalize("NFD", kw["word"])
    w = "".join(c for c in w if unicodedata.category(c) != "Mn")
    return (kw["type"], w.lower())


async def get_all_keywords() -> list[dict]:
    async with get_db() as db:
        result = await db.execute(
            text("SELECT id, word, type, active FROM voxidet_keywords")
        )
        rows = [dict(r) for r in result.mappings().all()]
    return sorted(rows, key=_kw_sort_key)


async def get_active_keywords() -> tuple[set[str], set[str]]:
    async with get_db() as db:
        result = await db.execute(
            text("SELECT word, type FROM voxidet_keywords WHERE active = 1")
        )
        human, voicemail = set(), set()
        for r in result.mappings().all():
            if r["type"] == "HUMAN":
                human.add(r["word"])
            else:
                voicemail.add(r["word"])
        return human, voicemail


async def add_keyword(word: str, type_: str) -> bool:
    try:
        async with get_db() as db:
            await db.execute(
                text("INSERT INTO voxidet_keywords (word, type) VALUES (:w, :t)"),
                {"w": word.lower().strip(), "t": type_},
            )
        return True
    except Exception:
        return False


async def delete_keyword(id_: int) -> None:
    async with get_db() as db:
        await db.execute(text("DELETE FROM voxidet_keywords WHERE id = :id"), {"id": id_})


async def toggle_keyword(id_: int) -> None:
    async with get_db() as db:
        await db.execute(
            text("UPDATE voxidet_keywords SET active = 1 - active WHERE id = :id"),
            {"id": id_},
        )
