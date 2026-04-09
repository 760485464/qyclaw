from __future__ import annotations

from backend.core.config import Settings
from sqlalchemy.orm import Session

from backend.core.models import Conversation, MCPBinding, MCPConnection, Skill, User
from backend.security.mcp_scope_resolver import (
    can_access_conversation,
    can_bind_connection_to_conversation,
    can_edit_connection,
    can_manage_binding,
    can_view_connection,
)
from backend.security.resource_policy import PolicyDecision
from backend.security.skill_scope_resolver import can_edit_skill, can_view_skill


def can_use_debug_exec(user: User, settings: Settings) -> tuple[bool, str | None]:
    security = settings.security
    if not security.debug_exec_enabled:
        return (False, 'debug_exec is disabled by security policy')
    if security.debug_exec_admin_only and not bool(user.is_admin):
        return (False, 'debug_exec is restricted to administrators')
    return (True, None)


def can_view_skill_resource(db: Session, user: User, skill: Skill) -> PolicyDecision:
    return can_view_skill(db, user, skill)


def can_edit_skill_resource(user: User, skill: Skill) -> PolicyDecision:
    return can_edit_skill(user, skill)


def can_view_mcp_connection(user: User, connection: MCPConnection | None) -> PolicyDecision:
    return can_view_connection(user, connection)


def can_edit_mcp_connection(user: User, connection: MCPConnection | None) -> PolicyDecision:
    return can_edit_connection(user, connection)


def can_access_conversation_resource(user: User, conversation: Conversation | None) -> PolicyDecision:
    return can_access_conversation(user, conversation)


def can_bind_mcp_connection(
    user: User,
    connection: MCPConnection | None,
    conversation: Conversation | None,
) -> PolicyDecision:
    return can_bind_connection_to_conversation(user, connection, conversation)


def can_manage_mcp_binding(
    user: User,
    binding: MCPBinding | None,
    connection: MCPConnection | None,
) -> PolicyDecision:
    return can_manage_binding(user, binding, connection)
