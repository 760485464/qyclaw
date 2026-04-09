from __future__ import annotations

from backend.core.models import Conversation, MCPBinding, MCPConnection, User
from backend.security.resource_policy import PolicyDecision, allow, deny


def can_view_connection(user: User, connection: MCPConnection | None) -> PolicyDecision:
    if not connection:
        return deny("Connection not found")
    if user.is_admin or connection.owner_user_id == user.id:
        return allow()
    return deny("Connection belongs to another user")


def can_edit_connection(user: User, connection: MCPConnection | None) -> PolicyDecision:
    return can_view_connection(user, connection)


def can_access_conversation(user: User, conversation: Conversation | None) -> PolicyDecision:
    if not conversation:
        return deny("Conversation not found")
    if user.is_admin or conversation.user_id == user.id:
        return allow()
    return deny("Conversation belongs to another user")


def can_bind_connection_to_conversation(
    user: User,
    connection: MCPConnection | None,
    conversation: Conversation | None,
) -> PolicyDecision:
    conversation_decision = can_access_conversation(user, conversation)
    if not conversation_decision.allowed:
        return conversation_decision
    connection_decision = can_view_connection(user, connection)
    if not connection_decision.allowed:
        return connection_decision
    if connection and connection.scope != "user":
        return deny("Only user-scoped MCP connections can be bound right now")
    return allow()


def can_manage_binding(
    user: User,
    binding: MCPBinding | None,
    connection: MCPConnection | None,
) -> PolicyDecision:
    if not binding:
        return deny("Binding not found")
    return can_view_connection(user, connection)
