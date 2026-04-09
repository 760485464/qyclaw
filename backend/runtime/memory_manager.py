from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from backend.core.models import ConversationMemory, MemoryAuditLog


def _audit_memory(
    db: Session,
    *,
    action: str,
    memory_kind: str,
    source_kind: str,
    actor_user_id: str | None = None,
    target_user_id: str | None = None,
    conversation_id: str | None = None,
    user_memory_id: str | None = None,
    tenant_id: str | None = None,
    detail: dict | None = None,
) -> None:
    db.add(
        MemoryAuditLog(
            id=str(uuid4()),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            conversation_id=conversation_id,
            user_memory_id=user_memory_id,
            memory_kind=memory_kind,
            action=action,
            source_kind=source_kind,
            detail=detail or {},
        )
    )


def render_memory_context(
    db: Session,
    conversation_id: str,
    user_id: str,
    *,
    actor_user_id: str | None = None,
    tenant_id: str | None = None,
) -> str:
    conversation_memory = db.get(ConversationMemory, conversation_id)
    if not conversation_memory or not conversation_memory.summary_md:
        return ""

    _audit_memory(
        db,
        action="read",
        memory_kind="conversation_summary",
        source_kind=conversation_memory.source_kind or "conversation",
        actor_user_id=actor_user_id or user_id,
        target_user_id=user_id,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        detail={"updated_from": conversation_memory.updated_from},
    )
    db.commit()
    return (
        "Use the following conversation summary as supporting context for the current turn.\n\n"
        "[Conversation Summary]\n"
        + conversation_memory.summary_md.strip()
    )


def update_conversation_memory(
    db: Session,
    conversation_id: str,
    user_content: str,
    assistant_content: str,
    message_id: str | None = None,
    updated_from: str = "turn",
    actor_user_id: str | None = None,
    tenant_id: str | None = None,
) -> ConversationMemory:
    memory = db.get(ConversationMemory, conversation_id)
    created = False
    if not memory:
        memory = ConversationMemory(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            created_by=actor_user_id,
            source_kind="conversation",
        )
        created = True

    user_text = " ".join((user_content or "").split())
    assistant_text = " ".join((assistant_content or "").split())
    if len(user_text) > 300:
        user_text = user_text[:297].rstrip() + "..."
    if len(assistant_text) > 400:
        assistant_text = assistant_text[:397].rstrip() + "..."

    parts: list[str] = []
    if memory.summary_md:
        parts.append(memory.summary_md.strip())
    if user_text:
        parts.append(f"User asked: {user_text}")
    if assistant_text:
        parts.append(f"Assistant answered: {assistant_text}")

    summary = "\n".join(part for part in parts if part).strip()
    if len(summary) > 2500:
        summary = summary[-2500:]

    memory.summary_md = summary or None
    memory.last_message_id = message_id
    memory.updated_from = updated_from
    memory.updated_by = actor_user_id
    memory.source_kind = "conversation"
    db.add(memory)
    _audit_memory(
        db,
        action="create" if created else "update",
        memory_kind="conversation_summary",
        source_kind=memory.source_kind,
        actor_user_id=actor_user_id,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        detail={"updated_from": updated_from, "message_id": message_id},
    )
    db.commit()
    db.refresh(memory)
    return memory
