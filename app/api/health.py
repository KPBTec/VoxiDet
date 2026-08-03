from fastapi import APIRouter
from app.db.clients import ping_db
from app.cache.client_cache import ping_redis

router = APIRouter()


@router.get("/health")
async def health():
    db_ok    = await ping_db()
    redis_ok = await ping_redis()
    status   = "ok" if db_ok and redis_ok else "degraded"
    return {
        "status": status,
        "db":     "ok" if db_ok    else "error",
        "cache":  "ok" if redis_ok else "error",
    }


@router.get("/")
async def root():
    return {"service": "VoxiDet", "version": "1.0.0"}
