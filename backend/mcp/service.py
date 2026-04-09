from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.models import MCPBinding, MCPConnection, MCPServerDefinition


def list_user_connections(db: Session, user_id: str, *, enabled_only: bool = False) -> list[MCPConnection]:
    stmt = (
        select(MCPConnection)
        .where(MCPConnection.owner_user_id == user_id)
        .order_by(MCPConnection.created_at.asc())
    )
    if enabled_only:
        stmt = stmt.where(MCPConnection.enabled.is_(True))
    return list(db.scalars(stmt).all())


def list_bound_connections(db: Session, conversation_id: str, user_id: str) -> list[MCPConnection]:
    _ = conversation_id
    # MCP now follows the user's own workspace. Any enabled user-owned connection
    # is considered available across that user's conversations.
    connections = list_user_connections(db, user_id, enabled_only=True)
    if connections:
        return connections
    stmt = (
        select(MCPConnection)
        .join(MCPBinding, MCPBinding.connection_id == MCPConnection.id)
        .where(MCPBinding.conversation_id == conversation_id)
        .where(MCPBinding.enabled.is_(True))
        .where(MCPConnection.enabled.is_(True))
        .where(MCPConnection.owner_user_id == user_id)
        .order_by(MCPConnection.created_at.asc())
    )
    return list(db.scalars(stmt).all())


def list_connection_capabilities(db: Session, conversation_id: str, user_id: str) -> list[dict]:
    connections = list_bound_connections(db, conversation_id, user_id)
    if not connections:
        return []
    server_keys = {c.server_key for c in connections}
    server_map = {
        row.key: row
        for row in db.scalars(select(MCPServerDefinition).where(MCPServerDefinition.key.in_(server_keys))).all()
    }
    items: list[dict] = []
    for connection in connections:
        server = server_map.get(connection.server_key)
        items.append(
            {
                "connection_id": connection.id,
                "server_key": connection.server_key,
                "display_name": connection.display_name,
                "scope": connection.scope,
                "server_type": server.server_type if server else "unknown",
                "enabled": bool(connection.enabled),
                "config": connection.config_json or {},
            }
        )
    return items
