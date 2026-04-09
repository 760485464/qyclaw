from __future__ import annotations

from pathlib import Path
import logging
import os
import random

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from backend.api.router import api_router
from backend.core.config import get_settings
from backend.core.database import Base, engine
from backend.core import models  # noqa: F401
from backend.runtime import queue_manager
from backend.runtime.scheduler import scheduler
from backend.ws.chat import router as ws_router
from backend.services.deepagents_service import deepagent_service
from backend.services.stream_events import stream_event_publisher
from backend.core.database import SessionLocal
from backend.core.models import ConversationSkillInstall, MCPServerDefinition, Skill, SkillGroupSkill, SystemToolDefinition

settings = get_settings()
logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_TOOLS: tuple[dict[str, object], ...] = (
    {
        "name": "terminal",
        "tool_type": "builtin",
        "risk_level": "critical",
        "approval_required": True,
        "enabled": True,
        "backend_support": {"deepagents": True, "claude": False},
        "container_required": True,
        "config_json": {"category": "execution"},
    },
    {
        "name": "web_search",
        "tool_type": "builtin",
        "risk_level": "medium",
        "approval_required": False,
        "enabled": True,
        "backend_support": {"deepagents": True, "claude": False},
        "container_required": False,
        "config_json": {"category": "network"},
    },
    {
        "name": "fetch_url",
        "tool_type": "builtin",
        "risk_level": "medium",
        "approval_required": False,
        "enabled": True,
        "backend_support": {"deepagents": True, "claude": False},
        "container_required": False,
        "config_json": {"category": "network"},
    },
    {
        "name": "internet_search",
        "tool_type": "builtin",
        "risk_level": "medium",
        "approval_required": False,
        "enabled": True,
        "backend_support": {"deepagents": True, "claude": False},
        "container_required": False,
        "config_json": {"category": "network"},
    },
)

DEFAULT_MCP_SERVERS: tuple[dict[str, object], ...] = (
    {
        "key": "custom_http",
        "display_name": "Custom HTTP MCP",
        "server_type": "custom",
        "enabled": True,
        "config_schema_json": {"fields": ["base_url", "headers", "timeout_seconds"]},
    },
    {
        "key": "github",
        "display_name": "GitHub MCP",
        "server_type": "github",
        "enabled": True,
        "config_schema_json": {"fields": ["owner", "repo", "api_base"]},
    },
    {
        "key": "postgres",
        "display_name": "Postgres MCP",
        "server_type": "database",
        "enabled": True,
        "config_schema_json": {"fields": ["dsn", "schema", "readonly"]},
    },
    {
        "key": "notion",
        "display_name": "Notion MCP",
        "server_type": "notion",
        "enabled": True,
        "config_schema_json": {"fields": ["workspace", "page_id"]},
    },
)


def _seed_system_tools() -> None:
    with SessionLocal() as db:
        existing = {
            row.name: row
            for row in db.query(SystemToolDefinition).all()
        }
        changed = False
        for item in DEFAULT_SYSTEM_TOOLS:
            name = str(item["name"])
            row = existing.get(name)
            if row is None:
                db.add(SystemToolDefinition(**item))
                changed = True
                continue
            if row.tool_type != item["tool_type"]:
                row.tool_type = str(item["tool_type"])
                changed = True
            if row.risk_level != item["risk_level"]:
                row.risk_level = str(item["risk_level"])
                changed = True
            if bool(row.approval_required) != bool(item["approval_required"]):
                row.approval_required = bool(item["approval_required"])
                changed = True
            if row.backend_support is None:
                row.backend_support = dict(item["backend_support"])  # type: ignore[arg-type]
                changed = True
            if row.config_json is None:
                row.config_json = dict(item["config_json"])  # type: ignore[arg-type]
                changed = True
            if row.container_required is None:
                row.container_required = bool(item["container_required"])
                changed = True
        if changed:
            db.commit()


def _seed_mcp_servers() -> None:
    with SessionLocal() as db:
        existing = {row.key: row for row in db.query(MCPServerDefinition).all()}
        changed = False
        for item in DEFAULT_MCP_SERVERS:
            key = str(item["key"])
            row = existing.get(key)
            if row is None:
                db.add(MCPServerDefinition(**item))
                changed = True
                continue
            if row.display_name != item["display_name"]:
                row.display_name = str(item["display_name"])
                changed = True
            if row.server_type != item["server_type"]:
                row.server_type = str(item["server_type"])
                changed = True
            if row.config_schema_json is None:
                row.config_schema_json = dict(item["config_schema_json"])  # type: ignore[arg-type]
                changed = True
        if changed:
            db.commit()


def _backfill_skill_scope() -> None:
    with SessionLocal() as db:
        skills = db.query(Skill).all()
        if not skills:
            return
        group_rows = db.query(SkillGroupSkill).all()
        groups_by_skill: dict[str, list[str]] = {}
        for row in group_rows:
            groups_by_skill.setdefault(row.skill_id, []).append(row.group_id)
        changed = False
        for skill in skills:
            desired_scope = "user"
            desired_group_id: str | None = None
            if skill.source_type == "agent":
                desired_scope = "user"
            elif skill.conversation_id:
                desired_scope = "conversation"
            elif groups_by_skill.get(skill.id):
                desired_scope = "group"
                desired_group_id = groups_by_skill[skill.id][0]
            elif skill.status == "published" and skill.is_public:
                desired_scope = "global"
            elif skill.scope in {"global", "group", "conversation", "user"}:
                desired_scope = skill.scope
                desired_group_id = skill.group_id if desired_scope == "group" else None
            if skill.scope != desired_scope:
                skill.scope = desired_scope
                changed = True
            if skill.group_id != desired_group_id:
                skill.group_id = desired_group_id
                changed = True
        if changed:
            db.commit()


def _backfill_conversation_skill_installs() -> None:
    with SessionLocal() as db:
        installs = db.query(ConversationSkillInstall).all()
        if not installs:
            return
        conversations = {row.id: row for row in db.query(models.Conversation).all()}
        skills = {row.id: row for row in db.query(Skill).all()}
        changed = False
        for install in installs:
            conversation = conversations.get(install.conversation_id)
            skill = skills.get(install.skill_id)
            desired_owner_id = None
            if conversation is not None:
                desired_owner_id = conversation.user_id
            elif skill is not None:
                desired_owner_id = skill.owner_user_id
            if desired_owner_id and install.owner_user_id != desired_owner_id:
                install.owner_user_id = desired_owner_id
                changed = True
        if changed:
            db.commit()

app = FastAPI(
    title=settings.app.name,
    version="0.1.0",
    debug=settings.app.debug,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(ws_router)
uploads_dir = Path("uploads")
uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS tool_name VARCHAR(128)"))
        conn.execute(text("ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS attachments_json JSONB"))
        conn.execute(text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS daemon_host VARCHAR(256)"))
        conn.execute(text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pending_interrupt_id VARCHAR(64)"))
        conn.execute(text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pending_schedule_json JSONB"))
        conn.execute(text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS execution_backend VARCHAR(32) DEFAULT 'deepagents'"))
        conn.execute(text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pinned_at TIMESTAMPTZ NULL"))
        conn.execute(text("UPDATE conversations SET execution_backend = 'deepagents' WHERE execution_backend IS NULL"))
        conn.execute(text("UPDATE conversations SET is_pinned = FALSE WHERE is_pinned IS NULL"))

        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS source_type VARCHAR(16)"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS status VARCHAR(32)"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS scope VARCHAR(16) DEFAULT 'user'"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS group_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS display_name VARCHAR(128)"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS description TEXT"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS is_public BOOLEAN DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS is_public_edit BOOLEAN DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS usage_count INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS cloned_from_skill_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS pending_comment TEXT"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS published_by VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS rejected_by VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS rejected_reason TEXT"))
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS conversation_skill_installs ("
                "id VARCHAR(36) PRIMARY KEY, "
                "owner_user_id VARCHAR(36), "
                "conversation_id VARCHAR(36) NOT NULL, "
                "skill_id VARCHAR(36) NOT NULL, "
                "installed_by_user_id VARCHAR(36), "
                "created_at TIMESTAMPTZ DEFAULT NOW(), "
                "updated_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_installs ADD COLUMN IF NOT EXISTS owner_user_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_installs ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_installs ADD COLUMN IF NOT EXISTS skill_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_installs ADD COLUMN IF NOT EXISTS installed_by_user_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_installs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_installs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_conversation_skill_installs_owner_user_id ON conversation_skill_installs(owner_user_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_conversation_skill_installs_conversation_id ON conversation_skill_installs(conversation_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_conversation_skill_installs_skill_id ON conversation_skill_installs(skill_id)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_skill_installs_conversation_skill ON conversation_skill_installs(conversation_id, skill_id)"))
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS conversation_skill_settings ("
                "id VARCHAR(36) PRIMARY KEY, "
                "owner_user_id VARCHAR(36), "
                "conversation_id VARCHAR(36) NOT NULL, "
                "skill_id VARCHAR(36) NOT NULL, "
                "enabled BOOLEAN DEFAULT TRUE, "
                "updated_by_user_id VARCHAR(36), "
                "created_at TIMESTAMPTZ DEFAULT NOW(), "
                "updated_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_settings ADD COLUMN IF NOT EXISTS owner_user_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_settings ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_settings ADD COLUMN IF NOT EXISTS skill_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_settings ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_settings ADD COLUMN IF NOT EXISTS updated_by_user_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_settings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_skill_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_conversation_skill_settings_owner_user_id ON conversation_skill_settings(owner_user_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_conversation_skill_settings_conversation_id ON conversation_skill_settings(conversation_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_conversation_skill_settings_skill_id ON conversation_skill_settings(skill_id)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_skill_settings_conversation_skill ON conversation_skill_settings(conversation_id, skill_id)"))
        conn.execute(text("ALTER TABLE IF EXISTS system_tool_definitions ADD COLUMN IF NOT EXISTS tool_type VARCHAR(32)"))
        conn.execute(text("ALTER TABLE IF EXISTS system_tool_definitions ADD COLUMN IF NOT EXISTS risk_level VARCHAR(16)"))
        conn.execute(text("ALTER TABLE IF EXISTS system_tool_definitions ADD COLUMN IF NOT EXISTS approval_required BOOLEAN DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE IF EXISTS system_tool_definitions ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE IF EXISTS system_tool_definitions ADD COLUMN IF NOT EXISTS backend_support JSONB DEFAULT '{}'::jsonb"))
        conn.execute(text("ALTER TABLE IF EXISTS system_tool_definitions ADD COLUMN IF NOT EXISTS container_required BOOLEAN DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE IF EXISTS system_tool_definitions ADD COLUMN IF NOT EXISTS config_json JSONB DEFAULT '{}'::jsonb"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_server_definitions ADD COLUMN IF NOT EXISTS key VARCHAR(64)"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_server_definitions ADD COLUMN IF NOT EXISTS display_name VARCHAR(128)"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_server_definitions ADD COLUMN IF NOT EXISTS server_type VARCHAR(32)"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_server_definitions ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_server_definitions ADD COLUMN IF NOT EXISTS config_schema_json JSONB DEFAULT '{}'::jsonb"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_connections ADD COLUMN IF NOT EXISTS owner_user_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_connections ADD COLUMN IF NOT EXISTS server_key VARCHAR(64)"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_connections ADD COLUMN IF NOT EXISTS scope VARCHAR(16) DEFAULT 'user'"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_connections ADD COLUMN IF NOT EXISTS group_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_connections ADD COLUMN IF NOT EXISTS display_name VARCHAR(128)"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_connections ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_connections ADD COLUMN IF NOT EXISTS credential_ref VARCHAR(255)"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_connections ADD COLUMN IF NOT EXISTS config_json JSONB DEFAULT '{}'::jsonb"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_bindings ADD COLUMN IF NOT EXISTS connection_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_bindings ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS mcp_bindings ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE IF EXISTS user_memories ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS user_memories ADD COLUMN IF NOT EXISTS created_by VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS user_memories ADD COLUMN IF NOT EXISTS updated_by VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS user_memories ADD COLUMN IF NOT EXISTS source_kind VARCHAR(32) DEFAULT 'manual'"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_memories ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_memories ADD COLUMN IF NOT EXISTS created_by VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_memories ADD COLUMN IF NOT EXISTS updated_by VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS conversation_memories ADD COLUMN IF NOT EXISTS source_kind VARCHAR(32) DEFAULT 'conversation'"))
        conn.execute(text("ALTER TABLE IF EXISTS memory_candidates ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS memory_candidates ADD COLUMN IF NOT EXISTS source_kind VARCHAR(32) DEFAULT 'auto_extract'"))
        conn.execute(text("ALTER TABLE IF EXISTS memory_audit_logs ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS memory_audit_logs ADD COLUMN IF NOT EXISTS actor_user_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS memory_audit_logs ADD COLUMN IF NOT EXISTS target_user_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS memory_audit_logs ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS memory_audit_logs ADD COLUMN IF NOT EXISTS user_memory_id VARCHAR(36)"))
        conn.execute(text("ALTER TABLE IF EXISTS memory_audit_logs ADD COLUMN IF NOT EXISTS memory_kind VARCHAR(32) DEFAULT 'user_long_term'"))
        conn.execute(text("ALTER TABLE IF EXISTS memory_audit_logs ADD COLUMN IF NOT EXISTS action VARCHAR(32) DEFAULT 'update'"))
        conn.execute(text("ALTER TABLE IF EXISTS memory_audit_logs ADD COLUMN IF NOT EXISTS source_kind VARCHAR(32) DEFAULT 'system'"))
        conn.execute(text("ALTER TABLE IF EXISTS memory_audit_logs ADD COLUMN IF NOT EXISTS detail JSONB DEFAULT '{}'::jsonb"))
        conn.execute(text("UPDATE skills SET usage_count = 0 WHERE usage_count IS NULL"))
        conn.execute(text("UPDATE skills SET scope = 'user' WHERE scope IS NULL"))
        conn.execute(
            text(
                "UPDATE skills SET usage_count = ("
                "SELECT COUNT(1) FROM conversation_messages "
                "WHERE conversation_messages.message_type = 'ToolMessage' "
                "AND conversation_messages.tool_name = skills.name)"
            )
        )

        docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
        hosts_cfg = docker_cfg.get("daemon_hosts")
        if isinstance(hosts_cfg, list):
            daemon_hosts = [str(h.get("host")) for h in hosts_cfg if isinstance(h, dict) and h.get("host")]
        else:
            single_host = docker_cfg.get("daemon_host")
            daemon_hosts = [str(single_host)] if single_host else []

        if daemon_hosts:
            rows = conn.execute(text("SELECT id FROM conversations WHERE daemon_host IS NULL")).fetchall()
            for row in rows:
                conn.execute(
                    text("UPDATE conversations SET daemon_host = :host WHERE id = :id"),
                    {"host": random.choice(daemon_hosts), "id": row[0]},
                )
    try:
        _seed_system_tools()
    except Exception as exc:  # noqa: BLE001
        logger.warning("seed_system_tools failed: %s", exc)
    try:
        _seed_mcp_servers()
    except Exception as exc:  # noqa: BLE001
        logger.warning("seed_mcp_servers failed: %s", exc)
    try:
        _backfill_skill_scope()
    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill_skill_scope failed: %s", exc)
    try:
        _backfill_conversation_skill_installs()
    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill_conversation_skill_installs failed: %s", exc)
    try:
        removed = deepagent_service.cleanup_orphan_containers()
        if removed:
            logger.info("Removed stale qyclaw containers: %s", removed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cleanup_orphan_containers failed: %s", exc)
    runtime_cfg = (settings.model_extra or {}).get("runtime", {}) or {}
    queue_manager.start(
        max_concurrency=int(runtime_cfg.get("max_concurrency", 2) or 2),
        max_retries=int(runtime_cfg.get("max_retries", 3) or 3),
    )
    scheduler.start(
        interval_seconds=int(runtime_cfg.get("scheduler_interval_seconds", 5) or 5),
    )


@app.get("/")
def root() -> dict:
    return {
        "service": settings.app.name,
        "status": "ok",
        "docs": "/docs",
        "health": "/api/v1/health",
    }


@app.on_event("shutdown")
def on_shutdown() -> None:
    stream_event_publisher.close_all()
    scheduler.stop()
    queue_manager.stop()
    deepagent_service.cleanup_all()
    try:
        removed = deepagent_service.cleanup_orphan_containers()
        if removed:
            logger.info("Removed stale qyclaw containers on shutdown: %s", removed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cleanup_orphan_containers on shutdown failed: %s", exc)


def run() -> None:
    import uvicorn

    reload_enabled = bool(settings.app.debug)
    if reload_enabled and os.name == "nt":
        logger.warning("Disabling uvicorn reload on Windows to avoid multiprocessing permission errors")
        reload_enabled = False

    reload_excludes: list[str] = []
    exclude_paths: list[Path] = []
    try:
        storage = settings.skill_storage
        reload_excludes.extend(
            [
                storage.userskills_dir,
                storage.preskills_dir,
                storage.skills_dir,
                storage.agentskills_dir,
                storage.conversationskills_dir,
            ]
        )
    except Exception:
        pass
    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    workspace_root = docker_cfg.get("workspace_root")
    if workspace_root:
        reload_excludes.append(str(workspace_root))

    resolved_excludes: list[str] = []
    cwd = Path.cwd()
    for item in reload_excludes:
        if not item:
            continue
        path = Path(str(item)).expanduser()
        if not path.is_absolute():
            path = (cwd / path).resolve()
        exclude_paths.append(path)
        try:
            rel = path.relative_to(cwd)
            rel_str = str(rel)
        except ValueError:
            # Keep relative pattern even if outside cwd (e.g., ../skills/**).
            rel_str = os.path.relpath(path, cwd)
        resolved_excludes.append(rel_str)
        resolved_excludes.append(str(Path(rel_str) / "**"))
    reload_excludes = resolved_excludes

    for path in exclude_paths:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    reload_dirs = [str(Path(__file__).resolve().parent)]
    logger.info("Uvicorn reload_dirs=%s reload_excludes=%s", reload_dirs, reload_excludes)

    uvicorn.run(
        "backend.main:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=reload_enabled,
        reload_dirs=reload_dirs,
        reload_excludes=reload_excludes or None,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    run()
