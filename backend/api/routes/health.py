from __future__ import annotations

from fastapi import APIRouter

from backend.core.config import get_settings
from backend.services.health import check_postgres

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def health() -> dict:
    settings = get_settings()
    postgres = check_postgres()
    deps = {"postgres": postgres}
    return {
        "service": settings.app.name,
        "env": settings.app.env,
        "status": "ok" if postgres["ok"] else "degraded",
        "dependencies": deps,
    }
