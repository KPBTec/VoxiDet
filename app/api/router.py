from fastapi import APIRouter

from app.api import health, install, amd, stream
from app.api.admin.router import router as admin_router
from app.config import settings

router = APIRouter()

router.include_router(health.router)
router.include_router(install.router)
router.include_router(amd.router)
router.include_router(stream.router)
router.include_router(admin_router, prefix=settings.ADMIN_PREFIX)
