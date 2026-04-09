from fastapi import APIRouter

from backend.api.routes import (
    auth,
    conversations,
    dashboard,
    gateway,
    health,
    mcp,
    scheduled_tasks,
    skills,
    users,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(dashboard.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(conversations.router)
api_router.include_router(mcp.router)
api_router.include_router(gateway.router)
api_router.include_router(scheduled_tasks.router)
api_router.include_router(skills.router)
