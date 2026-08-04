from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.config import settings

_url = (
    f"mysql+aiomysql://{settings.MYSQL_USER}:{settings.MYSQL_PASSWORD}"
    f"@{settings.MYSQL_HOST}:{settings.MYSQL_PORT}/{settings.MYSQL_DB}?charset=utf8mb4"
)

engine = create_async_engine(
    _url,
    pool_size=40,
    max_overflow=50,
    pool_pre_ping=False,   # aiomysql 0.2.0 no soporta ping() de SQLAlchemy 2.0
    pool_recycle=1800,
    echo=False,
    # Sin esto, una conexión que se cuelga (MySQL caído/red particionada) solo
    # cortaba cuando el backstop de 120s del worker timeout de gunicorn mataba
    # el proceso entero — mata este único request en 10s en vez de esperar ese
    # backstop mucho más agresivo.
    connect_args={"connect_timeout": 10},
)
# 90/worker (antes 30) — scripts/autotune.sh calcula MYSQL_MAX_CONNECTIONS con este
# mismo número (workers * 90 + 20) para que el tope de MySQL siga siendo el máximo
# real que la app puede llegar a abrir, no un número arbitrario. Si cambias esto,
# actualiza también el multiplicador en autotune.sh.

# Exportado para que cli/manage.py pueda usarlo directamente
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_db():
    """Context manager con commit automático y rollback en caso de error."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
