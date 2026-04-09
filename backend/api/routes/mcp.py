from __future__ import annotations

from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user
from backend.core.database import get_db
from backend.core.models import Conversation, MCPBinding, MCPConnection, MCPConnectionAuditLog, MCPServerDefinition, User
from backend.mcp.service import list_connection_capabilities, list_user_connections
from backend.security import (
    can_access_conversation_resource,
    can_bind_mcp_connection,
    can_edit_mcp_connection,
    can_manage_mcp_binding,
    require_allowed,
)
from backend.services.deepagents_service import deepagent_service

router = APIRouter(prefix="/mcp", tags=["mcp"])


class CreateConnectionRequest(BaseModel):
    server_key: str = Field(default="custom_http", min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    scope: str = Field(default="user", pattern="^(user|group|tenant)$")
    group_id: str | None = None
    credential_ref: str | None = None
    config_json: dict = Field(default_factory=dict)
    base_url: str | None = Field(default=None, max_length=1024)
    bearer_token: str | None = Field(default=None, max_length=4000)
    headers: dict = Field(default_factory=dict)
    query_params: dict = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=30, ge=1, le=300)
    enabled: bool = True


class UpdateConnectionRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    credential_ref: str | None = None
    config_json: dict | None = None
    base_url: str | None = Field(default=None, max_length=1024)
    bearer_token: str | None = Field(default=None, max_length=4000)
    headers: dict | None = None
    query_params: dict | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    enabled: bool | None = None


class BindConnectionRequest(BaseModel):
    connection_id: str
    enabled: bool = True


class UpdateBindingRequest(BaseModel):
    enabled: bool


def _serialize_server(server: MCPServerDefinition) -> dict:
    return {
        "id": server.id,
        "key": server.key,
        "display_name": server.display_name,
        "server_type": server.server_type,
        "enabled": bool(server.enabled),
        "config_schema_json": server.config_schema_json or {},
    }


def _serialize_connection(connection: MCPConnection) -> dict:
    config = connection.config_json or {}
    return {
        "id": connection.id,
        "owner_user_id": connection.owner_user_id,
        "server_key": connection.server_key,
        "scope": connection.scope,
        "group_id": connection.group_id,
        "display_name": connection.display_name,
        "enabled": bool(connection.enabled),
        "credential_ref": connection.credential_ref,
        "config_json": config,
        "base_url": config.get("base_url"),
        "bearer_token": config.get("bearer_token"),
        "headers": config.get("headers") or {},
        "query_params": config.get("query_params") or {},
        "timeout_seconds": config.get("timeout_seconds"),
        "created_at": connection.created_at.isoformat() if connection.created_at else None,
        "updated_at": connection.updated_at.isoformat() if connection.updated_at else None,
    }


def _serialize_binding(binding: MCPBinding) -> dict:
    return {
        "id": binding.id,
        "connection_id": binding.connection_id,
        "conversation_id": binding.conversation_id,
        "enabled": bool(binding.enabled),
        "created_at": binding.created_at.isoformat() if binding.created_at else None,
        "updated_at": binding.updated_at.isoformat() if binding.updated_at else None,
    }


def _serialize_workspace_binding(connection: MCPConnection, conversation_id: str) -> dict:
    return {
        "id": connection.id,
        "connection_id": connection.id,
        "conversation_id": conversation_id,
        "enabled": bool(connection.enabled),
        "created_at": connection.created_at.isoformat() if connection.created_at else None,
        "updated_at": connection.updated_at.isoformat() if connection.updated_at else None,
    }


def _serialize_audit_log(log: MCPConnectionAuditLog) -> dict:
    return {
        "id": log.id,
        "connection_id": log.connection_id,
        "actor_user_id": log.actor_user_id,
        "action": log.action,
        "detail": log.detail or {},
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


def _log(db: Session, connection_id: str, actor_user_id: str, action: str, detail: dict | None = None) -> None:
    db.add(
        MCPConnectionAuditLog(
            id=str(uuid4()),
            connection_id=connection_id,
            actor_user_id=actor_user_id,
            action=action,
            detail=detail or {},
        )
    )


def _normalize_custom_http_config(
    *,
    server_key: str,
    config_json: dict | None,
    base_url: str | None,
    bearer_token: str | None,
    headers: dict | None,
    query_params: dict | None,
    timeout_seconds: int | None,
) -> dict:
    config = dict(config_json or {})
    if server_key != "custom_http":
        return config
    resolved_url = str(base_url or config.get("base_url") or "").strip()
    if not resolved_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="HTTP MCP base_url is required")
    parsed = urlparse(resolved_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="HTTP MCP base_url must start with http:// or https://")
    config["base_url"] = resolved_url.rstrip("/")
    if bearer_token is not None:
        config["bearer_token"] = str(bearer_token).strip()
    else:
        config["bearer_token"] = str(config.get("bearer_token") or "").strip()
    config["headers"] = headers if headers is not None else (config.get("headers") or {})
    config["query_params"] = query_params if query_params is not None else (config.get("query_params") or {})
    config["timeout_seconds"] = int(timeout_seconds if timeout_seconds is not None else config.get("timeout_seconds") or 30)
    return config


@router.get("/servers")
def list_servers(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    _ = current_user
    rows = db.scalars(
        select(MCPServerDefinition)
        .where(MCPServerDefinition.enabled.is_(True))
        .where(MCPServerDefinition.key == "custom_http")
        .order_by(MCPServerDefinition.display_name.asc())
    ).all()
    return {"items": [_serialize_server(row) for row in rows]}


@router.get("/connections")
def list_connections(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(MCPConnection).order_by(MCPConnection.created_at.desc())).all()
    rows = [row for row in rows if can_edit_mcp_connection(current_user, row).allowed]
    return {"items": [_serialize_connection(row) for row in rows]}


@router.get("/audit")
def list_audit_logs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    connections = db.scalars(select(MCPConnection).order_by(MCPConnection.created_at.desc())).all()
    allowed_connection_ids = [
        row.id for row in connections if can_edit_mcp_connection(current_user, row).allowed
    ]
    if not allowed_connection_ids:
        return {"items": []}
    logs = db.scalars(
        select(MCPConnectionAuditLog)
        .where(MCPConnectionAuditLog.connection_id.in_(allowed_connection_ids))
        .order_by(MCPConnectionAuditLog.created_at.desc())
        .limit(100)
    ).all()
    return {"items": [_serialize_audit_log(item) for item in logs]}


@router.post("/connections")
def create_connection(
    payload: CreateConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    server = db.scalar(
        select(MCPServerDefinition).where(
            MCPServerDefinition.key == payload.server_key,
            MCPServerDefinition.enabled.is_(True),
        )
    )
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server definition not found")
    if payload.scope != "user":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only user scope is supported right now")
    normalized_config = _normalize_custom_http_config(
        server_key=payload.server_key,
        config_json=payload.config_json,
        base_url=payload.base_url,
        bearer_token=payload.bearer_token,
        headers=payload.headers,
        query_params=payload.query_params,
        timeout_seconds=payload.timeout_seconds,
    )
    row = MCPConnection(
        id=str(uuid4()),
        owner_user_id=current_user.id,
        server_key=payload.server_key,
        scope=payload.scope,
        group_id=payload.group_id,
        display_name=payload.display_name,
        enabled=payload.enabled,
        credential_ref=payload.credential_ref,
        config_json=normalized_config,
    )
    db.add(row)
    _log(db, row.id, current_user.id, "create", {"server_key": payload.server_key})
    db.commit()
    db.refresh(row)
    deepagent_service.invalidate_user_conversations(current_user.id, db=db)
    return {"item": _serialize_connection(row)}


@router.patch("/connections/{connection_id}")
def update_connection(
    connection_id: str,
    payload: UpdateConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(MCPConnection, connection_id)
    require_allowed(can_edit_mcp_connection(current_user, row), status_code=status.HTTP_404_NOT_FOUND)
    updates = payload.model_dump(exclude_unset=True)
    if any(key in updates for key in {"config_json", "base_url", "bearer_token", "headers", "query_params", "timeout_seconds"}):
        updates["config_json"] = _normalize_custom_http_config(
            server_key=row.server_key,
            config_json=updates.get("config_json", row.config_json),
            base_url=updates.get("base_url", (row.config_json or {}).get("base_url")),
            bearer_token=updates.get("bearer_token", (row.config_json or {}).get("bearer_token")),
            headers=updates.get("headers", (row.config_json or {}).get("headers")),
            query_params=updates.get("query_params", (row.config_json or {}).get("query_params")),
            timeout_seconds=updates.get("timeout_seconds", (row.config_json or {}).get("timeout_seconds")),
        )
    updates.pop("base_url", None)
    updates.pop("bearer_token", None)
    updates.pop("headers", None)
    updates.pop("query_params", None)
    updates.pop("timeout_seconds", None)
    for key, value in updates.items():
        setattr(row, key, value)
    db.add(row)
    _log(db, row.id, current_user.id, "update", {"fields": sorted(updates.keys())})
    db.commit()
    db.refresh(row)
    deepagent_service.invalidate_user_conversations(current_user.id, db=db)
    return {"item": _serialize_connection(row)}


@router.delete("/connections/{connection_id}")
def delete_connection(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(MCPConnection, connection_id)
    require_allowed(can_edit_mcp_connection(current_user, row), status_code=status.HTTP_404_NOT_FOUND)
    _log(db, row.id, current_user.id, "delete")
    db.delete(row)
    db.commit()
    deepagent_service.invalidate_user_conversations(current_user.id, db=db)
    return {"deleted": True, "id": connection_id}


@router.get("/conversations/{conversation_id}/bindings")
def list_bindings(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    require_allowed(can_access_conversation_resource(current_user, conversation), status_code=status.HTTP_404_NOT_FOUND)
    rows = list_user_connections(db, current_user.id, enabled_only=True)
    return {
        "items": [_serialize_workspace_binding(row, conversation_id) for row in rows],
        "capabilities": list_connection_capabilities(db, conversation_id, current_user.id),
    }


@router.post("/conversations/{conversation_id}/bindings")
def bind_connection(
    conversation_id: str,
    payload: BindConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    require_allowed(can_access_conversation_resource(current_user, conversation), status_code=status.HTTP_404_NOT_FOUND)
    connection = db.get(MCPConnection, payload.connection_id)
    require_allowed(
        can_bind_mcp_connection(current_user, connection, conversation),
        status_code=status.HTTP_404_NOT_FOUND,
    )
    connection.enabled = payload.enabled
    db.add(connection)
    _log(
        db,
        connection.id,
        current_user.id,
        "workspace_bind",
        {"conversation_id": conversation_id, "enabled": payload.enabled},
    )
    db.commit()
    db.refresh(connection)
    deepagent_service.invalidate_conversation(conversation_id)
    return {"item": _serialize_workspace_binding(connection, conversation_id)}


@router.patch("/bindings/{binding_id}")
def update_binding(
    binding_id: str,
    payload: UpdateBindingRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(MCPBinding, binding_id)
    if row:
        connection = db.get(MCPConnection, row.connection_id)
        require_allowed(can_manage_mcp_binding(current_user, row, connection), status_code=status.HTTP_404_NOT_FOUND)
        connection.enabled = payload.enabled
        db.add(connection)
        _log(db, connection.id, current_user.id, "workspace_update_binding", {"binding_id": binding_id, "enabled": payload.enabled})
        db.commit()
        db.refresh(connection)
        deepagent_service.invalidate_conversation(row.conversation_id)
        return {"item": _serialize_workspace_binding(connection, row.conversation_id)}
    connection = db.get(MCPConnection, binding_id)
    require_allowed(can_edit_mcp_connection(current_user, connection), status_code=status.HTTP_404_NOT_FOUND)
    connection.enabled = payload.enabled
    db.add(connection)
    _log(db, connection.id, current_user.id, "workspace_update_binding", {"binding_id": binding_id, "enabled": payload.enabled})
    db.commit()
    db.refresh(connection)
    deepagent_service.invalidate_user_conversations(current_user.id, db=db)
    return {"item": _serialize_workspace_binding(connection, "")}


@router.delete("/bindings/{binding_id}")
def delete_binding(
    binding_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(MCPBinding, binding_id)
    if row:
        connection = db.get(MCPConnection, row.connection_id)
        require_allowed(can_manage_mcp_binding(current_user, row, connection), status_code=status.HTTP_404_NOT_FOUND)
        connection.enabled = False
        db.add(connection)
        _log(db, connection.id, current_user.id, "workspace_unbind", {"conversation_id": row.conversation_id})
        db.commit()
        deepagent_service.invalidate_conversation(row.conversation_id)
        return {"deleted": True, "id": binding_id}
    connection = db.get(MCPConnection, binding_id)
    require_allowed(can_edit_mcp_connection(current_user, connection), status_code=status.HTTP_404_NOT_FOUND)
    connection.enabled = False
    db.add(connection)
    _log(db, connection.id, current_user.id, "workspace_unbind", {})
    db.commit()
    deepagent_service.invalidate_user_conversations(current_user.id, db=db)
    return {"deleted": True, "id": binding_id}
