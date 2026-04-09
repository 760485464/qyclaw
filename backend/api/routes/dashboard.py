from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user
from backend.core.database import get_db
from backend.core.models import (
    Conversation,
    ConversationMessage,
    MCPConnection,
    ScheduledTask,
    Skill,
    User,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _serialize_conversation(row: Conversation) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "model": row.model_name,
        "execution_backend": row.execution_backend,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "pending_interrupt_id": row.pending_interrupt_id,
    }


def _serialize_skill(row: Skill) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "display_name": row.display_name,
        "status": row.status,
        "scope": row.scope,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_connection(row: MCPConnection) -> dict:
    return {
        "id": row.id,
        "display_name": row.display_name,
        "server_key": row.server_key,
        "enabled": bool(row.enabled),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_task(row: ScheduledTask) -> dict:
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "title": row.title,
        "status": row.status,
        "next_run": row.next_run.isoformat() if row.next_run else None,
    }


@router.get("/summary")
def get_dashboard_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversations = db.scalars(
        select(Conversation)
        .where(Conversation.user_id == current_user.id)
        .order_by(desc(Conversation.updated_at), desc(Conversation.created_at))
        .limit(6)
    ).all()

    skills = db.scalars(
        select(Skill)
        .where(Skill.owner_user_id == current_user.id)
        .order_by(desc(Skill.updated_at), desc(Skill.created_at))
        .limit(6)
    ).all()

    mcp_connections = db.scalars(
        select(MCPConnection)
        .where(MCPConnection.owner_user_id == current_user.id)
        .order_by(desc(MCPConnection.updated_at), desc(MCPConnection.created_at))
        .limit(6)
    ).all()

    scheduled_tasks = db.scalars(
        select(ScheduledTask)
        .where(ScheduledTask.owner_user_id == current_user.id)
        .order_by(desc(ScheduledTask.updated_at), desc(ScheduledTask.created_at))
        .limit(6)
    ).all()

    conversation_ids = [item.id for item in conversations]
    recent_attachments: list[dict] = []
    if conversation_ids:
        attachment_messages = db.scalars(
            select(ConversationMessage)
            .where(
                ConversationMessage.conversation_id.in_(conversation_ids),
                ConversationMessage.attachments_json.is_not(None),
            )
            .order_by(desc(ConversationMessage.created_at))
            .limit(10)
        ).all()
        for message in attachment_messages:
            items = message.attachments_json.get("items") if isinstance(message.attachments_json, dict) else []
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                created_at = message.created_at or datetime.now(timezone.utc)
                recent_attachments.append(
                    {
                        "message_id": message.id,
                        "conversation_id": message.conversation_id,
                        "original_name": item.get("original_name"),
                        "saved_name": item.get("saved_name"),
                        "workspace_path": item.get("workspace_path"),
                        "uploaded_at": created_at.isoformat(),
                    }
                )
                if len(recent_attachments) >= 6:
                    break
            if len(recent_attachments) >= 6:
                break

    conversation_count = int(
        db.scalar(select(func.count(Conversation.id)).where(Conversation.user_id == current_user.id)) or 0
    )
    skill_count = int(
        db.scalar(select(func.count(Skill.id)).where(Skill.owner_user_id == current_user.id)) or 0
    )
    mcp_connection_count = int(
        db.scalar(select(func.count(MCPConnection.id)).where(MCPConnection.owner_user_id == current_user.id)) or 0
    )
    scheduled_task_count = int(
        db.scalar(select(func.count(ScheduledTask.id)).where(ScheduledTask.owner_user_id == current_user.id)) or 0
    )
    pending_interrupt_count = int(
        db.scalar(
            select(func.count(Conversation.id)).where(
                Conversation.user_id == current_user.id,
                Conversation.pending_interrupt_id.is_not(None),
            )
        )
        or 0
    )

    return {
        "recent_conversations": [_serialize_conversation(item) for item in conversations],
        "recent_skills": [_serialize_skill(item) for item in skills],
        "recent_mcp_connections": [_serialize_connection(item) for item in mcp_connections],
        "recent_scheduled_tasks": [_serialize_task(item) for item in scheduled_tasks],
        "recent_attachments": recent_attachments,
        "stats": {
            "conversation_count": conversation_count,
            "skill_count": skill_count,
            "mcp_connection_count": mcp_connection_count,
            "scheduled_task_count": scheduled_task_count,
            "pending_interrupt_count": pending_interrupt_count,
        },
        "gateway": {
            "status": "not_configured",
            "summary": "Platform gateway backend is not implemented yet.",
        },
    }
