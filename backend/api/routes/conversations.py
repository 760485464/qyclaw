from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import os
import queue
import re
import random
from time import perf_counter
import traceback
from uuid import uuid4
import threading
import shutil
from typing import Any, Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, or_, select, update
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user
from backend.agent_backends import get_backend, normalize_backend_name
from backend.core.config import get_settings
from backend.core.database import SessionLocal, get_db
from backend.core.models import BackendRunLog, Conversation, ConversationMessage, ConversationSkillInstall, ConversationSkillSetting, MCPBinding, MCPConnection, ScheduledTask, Skill, User
from backend.mcp.service import list_connection_capabilities, list_user_connections
from backend.core.security import decode_access_token
from backend.i18n import t
from backend.security import can_use_debug_exec
from backend.runtime.backend_router import resolve_backend_name
from backend.runtime.dispatcher import register_handler
from backend.runtime.queue_manager import queue_manager
from backend.runtime.runtime_registry import runtime_registry
from backend.runtime.task_execution import (
    execute_interrupt_resume_and_persist,
    execute_scheduled_task_and_persist,
    execute_user_message_and_persist,
)
from backend.runtime.work_items import WorkItem
from backend.services.office_extract import ALLOWED_OFFICE_EXTENSIONS, extract_office_to_markdown
from backend.services.deepagents_service import deepagent_service
from backend.services.stream_events import stream_event_publisher

router = APIRouter(prefix="/conversations", tags=["conversations"])
logger = logging.getLogger(__name__)


class CreateConversationRequest(BaseModel):
    title: str = Field(default_factory=lambda: t("conversation.default_title"))
    model: str = "default"
    execution_backend: str | None = Field(default=None, pattern="^(deepagents|claude)$")
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class UpdateConversationRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    is_pinned: bool | None = None
    execution_backend: str | None = Field(default=None, pattern="^(deepagents|claude)$")


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1)


class InterruptDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(allow|reject|allow_all)$")

class DebugExecRequest(BaseModel):
    command: str = Field(min_length=1)


def _build_scheduled_task_title(reminder_text: str) -> str:
    text = str(reminder_text or "").strip()
    if not text:
        return "Reminder"
    if len(text) <= 40:
        return f"Reminder: {text}"
    return f"Reminder: {text[:37].rstrip()}..."


def _unit_to_seconds(unit: str) -> int | None:
    value = str(unit or "").lower()
    if value in {"秒", "秒钟", "second", "seconds", "sec", "secs"}:
        return 1
    if value in {"分", "分钟", "minute", "minutes", "min", "mins"}:
        return 60
    if value in {"小时", "个小时", "hour", "hours"}:
        return 3600
    if value in {"天", "day", "days"}:
        return 86400
    return None


def _parse_reminder_request(content: str) -> dict | None:
    text = str(content or "").strip()
    if not text:
        return None

    recurring_patterns = [
        r"^(?P<delay>\d+)\s*(?P<delay_unit>秒钟?|秒|分钟|分(?:钟)?|小时|个小时|天)\s*后每隔\s*(?P<interval>\d+)\s*(?P<interval_unit>秒钟?|秒|分钟|分(?:钟)?|小时|个小时|天)\s*(?:提醒我|提醒|叫我|通知我)\s*(?P<body>.*)$",
        r"^每隔\s*(?P<interval>\d+)\s*(?P<interval_unit>秒钟?|秒|分钟|分(?:钟)?|小时|个小时|天)\s*(?:提醒我|提醒|叫我|通知我)\s*(?P<body>.*)$",
        r"^in\s+(?P<delay>\d+)\s*(?P<delay_unit>seconds?|secs?|minutes?|mins?|hours?|days?)\s+remind me every\s+(?P<interval>\d+)\s*(?P<interval_unit>seconds?|secs?|minutes?|mins?|hours?|days?)\s*(?P<body>.*)$",
        r"^remind me every\s+(?P<interval>\d+)\s*(?P<interval_unit>seconds?|secs?|minutes?|mins?|hours?|days?)\s*(?P<body>.*)$",
    ]
    chinese_patterns = [
        r"^(?P<delay>\d+)\s*(?P<unit>秒钟?|秒|分钟|分(?:钟)?|小时|个小时|天)\s*后\s*(?:提醒我|提醒|叫我|通知我)\s*(?P<body>.*)$",
        r"^(?P<delay>\d+)\s*(?P<unit>秒钟?|秒|分钟|分(?:钟)?|小时|个小时|天)\s*(?:提醒我|提醒|叫我|通知我)\s*(?P<body>.*)$",
        r"^(?:提醒我|提醒|叫我|通知我)\s*(?:在)?\s*(?P<delay>\d+)\s*(?P<unit>秒钟?|秒|分钟|分(?:钟)?|小时|个小时|天)\s*后\s*(?P<body>.*)$",
    ]
    english_patterns = [
        r"^remind me(?: to)?\s+(?P<body>.+?)\s+in\s+(?P<delay>\d+)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|days?)$",
        r"^in\s+(?P<delay>\d+)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|days?)\s+remind me(?: to)?\s*(?P<body>.*)$",
    ]

    match = None
    for pattern in recurring_patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            delay = int(match.groupdict().get("delay") or 0)
            delay_unit = _unit_to_seconds(str(match.groupdict().get("delay_unit") or "秒"))
            interval = int(match.group("interval"))
            interval_unit = _unit_to_seconds(str(match.group("interval_unit") or "秒"))
            body = str(match.groupdict().get("body") or "").strip() or text
            if interval <= 0 or interval_unit is None:
                return None
            delay_seconds = delay * delay_unit if delay > 0 and delay_unit else interval * interval_unit
            next_run = datetime.now(timezone.utc) + timedelta(seconds=max(1, delay_seconds))
            return {
                "title": _build_scheduled_task_title(body),
                "prompt": body,
                "schedule_type": "interval",
                "schedule_value": str(interval * interval_unit),
                "run_at": next_run,
            }

    for pattern in chinese_patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            break
    if not match:
        for pattern in english_patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if match:
                break
    if not match:
        return None

    delay = int(match.group("delay"))
    unit = str(match.group("unit") or "").lower()
    body = str(match.groupdict().get("body") or "").strip()
    multiplier = _unit_to_seconds(unit)
    if multiplier is None or delay <= 0:
        return None

    reminder_text = body or text
    run_at = datetime.now(timezone.utc) + timedelta(seconds=delay * multiplier)
    return {
        "title": _build_scheduled_task_title(reminder_text),
        "prompt": reminder_text,
        "schedule_type": "once",
        "schedule_value": run_at.isoformat(),
        "run_at": run_at,
    }


def _normalize_reminder_text(content: str) -> str:
    text = str(content or "").strip()
    return re.sub(r"\s+", " ", text)


def _unit_to_seconds(unit: str) -> int | None:
    value = str(unit or "").lower()
    if value in {"\u79d2", "\u79d2\u949f", "second", "seconds", "sec", "secs"}:
        return 1
    if value in {"\u5206", "\u5206\u949f", "minute", "minutes", "min", "mins"}:
        return 60
    if value in {"\u5c0f\u65f6", "\u4e2a\u5c0f\u65f6", "hour", "hours"}:
        return 3600
    if value in {"\u5929", "day", "days"}:
        return 86400
    return None


def _interval_to_cron(interval: int, unit_seconds: int) -> str | None:
    total_seconds = int(interval or 0) * int(unit_seconds or 0)
    if total_seconds < 60:
        return None
    if total_seconds % 86400 == 0:
        days = total_seconds // 86400
        return f"0 0 */{days} * *"
    if total_seconds % 3600 == 0:
        hours = total_seconds // 3600
        return f"0 */{hours} * * *"
    if total_seconds % 60 == 0:
        minutes = total_seconds // 60
        return f"*/{minutes} * * * *"
    return None


def _parse_interval_only(text: str) -> dict | None:
    normalized = _normalize_reminder_text(text)
    if not normalized:
        return None
    patterns = [
        r"^\u6bcf\u9694\s*(?P<interval>\d+)\s*(?P<unit>\u79d2(?:\u949f)?|\u5206(?:\u949f)?|\u5c0f\u65f6|\u4e2a\u5c0f\u65f6|\u5929)$",
        r"^(?P<interval>\d+)\s*(?P<unit>\u79d2(?:\u949f)?|\u5206(?:\u949f)?|\u5c0f\u65f6|\u4e2a\u5c0f\u65f6|\u5929)$",
        r"^every\s+(?P<interval>\d+)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|days?)$",
        r"^(?P<interval>\d+)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|days?)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        interval = int(match.group("interval"))
        unit_seconds = _unit_to_seconds(match.group("unit"))
        if interval <= 0 or unit_seconds is None:
            return None
        next_run = datetime.now(timezone.utc) + timedelta(seconds=max(1, interval * unit_seconds))
        cron_expression = _interval_to_cron(interval, unit_seconds)
        return {
            "schedule_type": "cron" if cron_expression else "interval",
            "schedule_value": cron_expression or str(interval * unit_seconds),
            "run_at": next_run,
            "interval_label": normalized,
        }
    return None


def _parse_pending_schedule_prompt(content: str) -> dict | None:
    text = _normalize_reminder_text(content)
    if not text:
        return None
    recurring_markers = [
        "\u6bcf\u9694\u4e00\u6bb5\u65f6\u95f4",
        "\u5b9a\u671f",
        "\u6bcf\u8fc7\u4e00\u6bb5\u65f6\u95f4",
        "every once in a while",
        "periodically",
        "regularly",
    ]
    if not any(marker in text.lower() if marker.isascii() else marker in text for marker in recurring_markers):
        return None
    prompt = text
    if "\u52b1\u5fd7" in text:
        prompt = "\u53d1\u4e00\u53e5\u52b1\u5fd7\u7684\u8bdd"
    elif "\u7b11\u8bdd" in text:
        prompt = "\u53d1\u4e00\u4e2a\u7b11\u8bdd"
    elif "\u63d0\u9192" in text:
        prompt = text
    return {
        "title": _build_scheduled_task_title(prompt),
        "prompt": prompt,
        "awaiting": "interval",
        "original_request": text,
    }


def _create_scheduled_task(
    db: Session,
    *,
    current_user: User,
    conversation_id: str,
    task_payload: dict,
) -> ScheduledTask:
    task = ScheduledTask(
        owner_user_id=current_user.id,
        conversation_id=conversation_id,
        title=task_payload["title"],
        prompt=task_payload["prompt"],
        schedule_type=task_payload["schedule_type"],
        schedule_value=task_payload["schedule_value"],
        status="active",
        next_run=task_payload["run_at"],
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _append_system_message(
    db: Session,
    *,
    conversation_id: str,
    content: str,
    user_id: str,
) -> ConversationMessage:
    message = ConversationMessage(
        conversation_id=conversation_id,
        sender_user_id=None,
        sender_role="assistant",
        message_type="SystemMessage",
        message_status="done",
        content_md=content,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    _publish_messages(conversation_id, user_id, [_serialize_message(message)])
    return message


def _parse_reminder_request(content: str) -> dict | None:
    text = _normalize_reminder_text(content)
    if not text:
        return None

    recurring_patterns = [
        r"^(?P<delay>\d+)\s*(?P<delay_unit>\u79d2(?:\u949f)?|\u5206(?:\u949f)?|\u5c0f\u65f6|\u4e2a\u5c0f\u65f6|\u5929)\u540e\u6bcf\u9694\s*(?P<interval>\d+)\s*(?P<interval_unit>\u79d2(?:\u949f)?|\u5206(?:\u949f)?|\u5c0f\u65f6|\u4e2a\u5c0f\u65f6|\u5929)\s*(?:\u63d0\u9192\u6211|\u63d0\u9192|\u53eb\u6211|\u901a\u77e5\u6211)\s*(?P<body>.*)$",
        r"^\u6bcf\u9694\s*(?P<interval>\d+)\s*(?P<interval_unit>\u79d2(?:\u949f)?|\u5206(?:\u949f)?|\u5c0f\u65f6|\u4e2a\u5c0f\u65f6|\u5929)\s*(?:\u63d0\u9192\u6211|\u63d0\u9192|\u53eb\u6211|\u901a\u77e5\u6211)\s*(?P<body>.*)$",
        r"^in\s+(?P<delay>\d+)\s*(?P<delay_unit>seconds?|secs?|minutes?|mins?|hours?|days?)\s+remind me every\s+(?P<interval>\d+)\s*(?P<interval_unit>seconds?|secs?|minutes?|mins?|hours?|days?)\s*(?P<body>.*)$",
        r"^remind me every\s+(?P<interval>\d+)\s*(?P<interval_unit>seconds?|secs?|minutes?|mins?|hours?|days?)\s*(?P<body>.*)$",
    ]
    one_time_patterns = [
        r"^(?P<delay>\d+)\s*(?P<unit>\u79d2(?:\u949f)?|\u5206(?:\u949f)?|\u5c0f\u65f6|\u4e2a\u5c0f\u65f6|\u5929)\u540e\s*(?:\u63d0\u9192\u6211|\u63d0\u9192|\u53eb\u6211|\u901a\u77e5\u6211)\s*(?P<body>.*)$",
        r"^(?P<delay>\d+)\s*(?P<unit>\u79d2(?:\u949f)?|\u5206(?:\u949f)?|\u5c0f\u65f6|\u4e2a\u5c0f\u65f6|\u5929)\s*(?:\u540e)?\s*(?:\u63d0\u9192\u6211|\u63d0\u9192|\u53eb\u6211|\u901a\u77e5\u6211)\s*(?P<body>.*)$",
        r"^(?:\u63d0\u9192\u6211|\u63d0\u9192|\u53eb\u6211|\u901a\u77e5\u6211)\s*(?P<delay>\d+)\s*(?P<unit>\u79d2(?:\u949f)?|\u5206(?:\u949f)?|\u5c0f\u65f6|\u4e2a\u5c0f\u65f6|\u5929)\u540e\s*(?P<body>.*)$",
        r"^remind me(?: to)?\s+(?P<body>.+?)\s+in\s+(?P<delay>\d+)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|days?)$",
        r"^in\s+(?P<delay>\d+)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|days?)\s+remind me(?: to)?\s*(?P<body>.*)$",
    ]

    for pattern in recurring_patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        delay = int(match.groupdict().get("delay") or 0)
        delay_unit = _unit_to_seconds(str(match.groupdict().get("delay_unit") or "\u79d2"))
        interval = int(match.group("interval"))
        interval_unit = _unit_to_seconds(str(match.group("interval_unit") or "\u79d2"))
        body = str(match.groupdict().get("body") or "").strip() or text
        if interval <= 0 or interval_unit is None:
            return None
        delay_seconds = delay * delay_unit if delay > 0 and delay_unit else interval * interval_unit
        next_run = datetime.now(timezone.utc) + timedelta(seconds=max(1, delay_seconds))
        cron_expression = None if delay > 0 else _interval_to_cron(interval, interval_unit)
        return {
            "title": _build_scheduled_task_title(body),
            "prompt": body,
            "schedule_type": "cron" if cron_expression else "interval",
            "schedule_value": cron_expression or str(interval * interval_unit),
            "run_at": next_run,
        }

    for pattern in one_time_patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        delay = int(match.group("delay"))
        unit = str(match.group("unit") or "").lower()
        body = str(match.groupdict().get("body") or "").strip()
        multiplier = _unit_to_seconds(unit)
        if multiplier is None or delay <= 0:
            return None
        reminder_text = body or text
        run_at = datetime.now(timezone.utc) + timedelta(seconds=delay * multiplier)
        return {
            "title": _build_scheduled_task_title(reminder_text),
            "prompt": reminder_text,
            "schedule_type": "once",
            "schedule_value": run_at.isoformat(),
            "run_at": run_at,
        }

    return None


def _serialize_message(m: ConversationMessage) -> dict:
    created_at = m.created_at
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "sender_role": m.sender_role,
        "message_type": m.message_type,
        "tool_name": m.tool_name,
        "message_status": m.message_status,
        "content_md": m.content_md,
        "attachments_json": m.attachments_json,
        "input_tokens": m.input_tokens,
        "output_tokens": m.output_tokens,
        "total_tokens": m.total_tokens,
        "run_duration_ms": m.run_duration_ms,
        "created_at": created_at.isoformat(),
    }


def _extract_attachments(payload: object) -> list[dict]:
    if not payload:
        return []
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
        return []
    if isinstance(payload, list):
        return [i for i in payload if isinstance(i, dict)]
    return []


def _serialize_conversation(c: Conversation) -> dict:
    return {
        "id": c.id,
        "title": c.title,
        "model": c.model_name,
        "execution_backend": getattr(c, "execution_backend", "deepagents"),
        "container_status": c.container_status,
        "daemon_host": c.daemon_host,
        "pending_interrupt_id": c.pending_interrupt_id,
        "pending_schedule": getattr(c, "pending_schedule_json", None),
        "is_pinned": bool(getattr(c, "is_pinned", False)),
        "pinned_at": c.pinned_at.isoformat() if c.pinned_at else None,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


def _serialize_skill_chip(
    skill: Skill,
    install: ConversationSkillInstall | None = None,
    conversation_enabled: bool | None = None,
) -> dict:
    return {
        "id": skill.id,
        "name": skill.name,
        "display_name": skill.display_name,
        "description": skill.description,
        "source_type": skill.source_type,
        "status": skill.status,
        "scope": skill.scope,
        "installed": install is not None,
        "conversation_enabled": bool(conversation_enabled) if conversation_enabled is not None else install is not None,
        "install_id": install.id if install else None,
        "installed_at": install.created_at.isoformat() if install and install.created_at else None,
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }


def _serialize_binding_summary(binding: MCPBinding, connection: MCPConnection | None) -> dict:
    return {
        "id": binding.id,
        "connection_id": binding.connection_id,
        "enabled": bool(binding.enabled),
        "display_name": connection.display_name if connection else binding.connection_id,
        "server_key": connection.server_key if connection else None,
        "updated_at": binding.updated_at.isoformat() if binding.updated_at else None,
    }


def _serialize_workspace_binding_summary(connection: MCPConnection) -> dict:
    return {
        "id": connection.id,
        "connection_id": connection.id,
        "enabled": bool(connection.enabled),
        "display_name": connection.display_name,
        "server_key": connection.server_key,
        "updated_at": connection.updated_at.isoformat() if connection.updated_at else None,
    }


def _serialize_connection_summary(connection: MCPConnection, binding: MCPBinding | None = None) -> dict:
    config = connection.config_json or {}
    return {
        "id": connection.id,
        "connection_id": connection.id,
        "display_name": connection.display_name,
        "server_key": connection.server_key,
        "enabled": bool(connection.enabled),
        "base_url": config.get("base_url"),
        "binding_id": binding.id if binding else connection.id,
        "conversation_enabled": bool(binding.enabled) if binding else bool(connection.enabled),
        "updated_at": (
            binding.updated_at.isoformat()
            if binding and binding.updated_at
            else connection.updated_at.isoformat() if connection.updated_at else None
        ),
    }


def _serialize_task_summary(task: ScheduledTask) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "prompt": task.prompt,
        "schedule_type": task.schedule_type,
        "schedule_value": task.schedule_value,
        "status": task.status,
        "next_run": task.next_run.isoformat() if task.next_run else None,
        "last_run": task.last_run.isoformat() if task.last_run else None,
        "last_result": task.last_result,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def _serialize_backend_run(log) -> dict:
    return {
        "id": log.id,
        "work_kind": log.work_kind,
        "requested_backend": log.requested_backend,
        "actual_backend": log.actual_backend,
        "fallback_backend": log.fallback_backend,
        "status": log.status,
        "latency_ms": log.latency_ms,
        "error": log.error,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


def _publish_messages(conversation_id: str, user_id: str, messages: list[dict]) -> None:
    for msg in messages:
        stream_event_publisher.publish_conversation_event(
            conversation_id=conversation_id,
            user_id=user_id,
            event_type="message.created",
            payload=msg,
        )


def _publish_message_event(conversation_id: str, user_id: str, message: dict, event_type: str) -> None:
    stream_event_publisher.publish_conversation_event(
        conversation_id=conversation_id,
        user_id=user_id,
        event_type=event_type,
        payload=message,
    )


def _backend_for_conversation(conversation: Conversation):
    return get_backend(getattr(conversation, "execution_backend", "deepagents"))


def _make_progress_handler(
    conversation_id: str,
    user_id: str,
    db: Session,
    started: float,
) -> tuple[Callable[[dict], None], Callable[..., ConversationMessage | None], dict]:
    state: dict[str, Any] = {
        "ai_message": None,
        "ai_buffer": "",
        "last_flush": perf_counter(),
    }

    def flush_ai(
        force: bool = False,
        final_status: str | None = None,
        tokens: dict[str, int] | None = None,
        run_duration_ms: int | None = None,
    ) -> ConversationMessage | None:
        ai_message: ConversationMessage | None = state["ai_message"]
        buffer = state["ai_buffer"]
        now = perf_counter()
        should_flush = force or len(buffer) >= 200 or (now - state["last_flush"]) >= 0.8
        if not ai_message and not buffer:
            return None
        if not ai_message:
            content = buffer
            if not content and not force:
                return None
            ai_message = ConversationMessage(
                conversation_id=conversation_id,
                sender_user_id=None,
                sender_role="assistant",
                message_type="AIMessage",
                message_status="streaming",
                content_md=content,
                run_duration_ms=int((perf_counter() - started) * 1000),
            )
            db.add(ai_message)
            db.commit()
            db.refresh(ai_message)
            _publish_message_event(conversation_id, user_id, _serialize_message(ai_message), "message.created")
            state["ai_message"] = ai_message
            state["ai_buffer"] = ""
            state["last_flush"] = now
            return ai_message

        if not should_flush and not final_status:
            return ai_message

        if buffer:
            ai_message.content_md = (ai_message.content_md or "") + buffer
            state["ai_buffer"] = ""
        if final_status:
            ai_message.message_status = final_status
        if tokens:
            ai_message.input_tokens = tokens.get("input_tokens", ai_message.input_tokens or 0)
            ai_message.output_tokens = tokens.get("output_tokens", ai_message.output_tokens or 0)
            ai_message.total_tokens = tokens.get("total_tokens", ai_message.total_tokens or 0)
        if run_duration_ms is not None:
            ai_message.run_duration_ms = run_duration_ms
        db.add(ai_message)
        db.commit()
        db.refresh(ai_message)
        _publish_message_event(conversation_id, user_id, _serialize_message(ai_message), "message.updated")
        state["last_flush"] = now
        return ai_message

    def on_progress(event: dict) -> None:
        event_type = event.get("type")
        if event_type == "tool_output":
            tool_name = event.get("tool_name")
            tool_content = str(event.get("content") or "")
            tool_status = (
                "failed"
                if "Command failed with exit code" in tool_content or "Error executing" in tool_content
                else "done"
            )
            tool_message = ConversationMessage(
                conversation_id=conversation_id,
                sender_user_id=None,
                sender_role="assistant",
                message_type="ToolMessage",
                tool_name=tool_name,
                message_status=tool_status,
                content_md=tool_content,
                run_duration_ms=int((perf_counter() - started) * 1000),
            )
            db.add(tool_message)
            if tool_name:
                db.execute(
                    update(Skill)
                    .where(Skill.name == str(tool_name))
                    .values(usage_count=Skill.usage_count + 1)
                )
            db.commit()
            db.refresh(tool_message)
            _publish_message_event(conversation_id, user_id, _serialize_message(tool_message), "message.created")
            return

        if event_type == "ai_chunk":
            chunk = str(event.get("content") or "")
            if not chunk:
                return
            state["ai_buffer"] = state["ai_buffer"] + chunk
            flush_ai(force=False)

    return on_progress, flush_ai, state


def _normalize_tool_output(tool_output: object) -> tuple[str | None, str]:
    if isinstance(tool_output, dict):
        tool_name = tool_output.get("tool_name")
        content = tool_output.get("content") or ""
        return (str(tool_name) if tool_name else None, str(content))
    text = str(tool_output or "")
    return (None, text)


def _format_exception(exc: Exception) -> str:
    msg = str(exc).strip()
    exc_name = exc.__class__.__name__
    if msg:
        return f"{exc_name}: {msg}"

    cause = getattr(exc, "__cause__", None)
    if cause:
        cause_msg = str(cause).strip()
        cause_name = cause.__class__.__name__
        if cause_msg:
            return f"{exc_name} (caused by {cause_name}: {cause_msg})"
        return f"{exc_name} (caused by {cause_name})"

    tb = "".join(traceback.format_exception_only(exc.__class__, exc)).strip()
    return tb or exc_name


def _user_from_token(token: str | None, db: Session) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="Token is required")
    try:
        payload = decode_access_token(token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    user = db.get(User, payload.get("sub"))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if user.is_blocked:
        raise HTTPException(status_code=403, detail="User is blocked")
    return user


def _resume_interrupt_and_persist(
    conversation: Conversation,
    interrupt_id: str,
    decision: str,
    user_id: str,
    db: Session,
) -> dict:
    started = perf_counter()
    on_progress, flush_ai, _state = _make_progress_handler(conversation.id, user_id, db, started)
    backend = _backend_for_conversation(conversation)
    try:
        result = backend.resume_interrupt(
            conversation_id=conversation.id,
            interrupt_id=interrupt_id,
            decision=decision,
            on_progress=on_progress,
        )
        elapsed_ms = int((perf_counter() - started) * 1000)

        if result.get("interrupted"):
            flush_ai(
                force=True,
                final_status="streaming",
                tokens={
                    "input_tokens": int(result.get("input_tokens", 0) or 0),
                    "output_tokens": int(result.get("output_tokens", 0) or 0),
                    "total_tokens": int(result.get("total_tokens", 0) or 0),
                },
                run_duration_ms=elapsed_ms,
            )

            interrupt_text = backend.format_interrupt_message(result["interrupts"])
            conversation.pending_interrupt_id = result["interrupt_id"]
            db.add(conversation)
            interrupt_message = ConversationMessage(
                conversation_id=conversation.id,
                sender_user_id=None,
                sender_role="assistant",
                message_type="SystemMessage",
                message_status="pending",
                content_md=interrupt_text,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                run_duration_ms=elapsed_ms,
            )
            db.add(interrupt_message)
            db.commit()
            db.refresh(interrupt_message)
            _publish_messages(conversation.id, user_id, [_serialize_message(interrupt_message)])
            return {
                "accepted": True,
                "requires_interrupt_decision": True,
                "interrupt_id": result["interrupt_id"],
            }

        ai_status = "done"
        ai_type = "AIMessage"
        if result.get("rejected"):
            ai_status = "cancelled"
            ai_type = "SystemMessage"

        ai_message = flush_ai(
            force=True,
            final_status=ai_status,
            tokens={
                "input_tokens": int(result.get("input_tokens", 0) or 0),
                "output_tokens": int(result.get("output_tokens", 0) or 0),
                "total_tokens": int(result.get("total_tokens", 0) or 0),
            },
            run_duration_ms=elapsed_ms,
        )
        if not ai_message:
            assistant_message = ConversationMessage(
                conversation_id=conversation.id,
                sender_user_id=None,
                sender_role="assistant",
                message_type=ai_type,
                message_status=ai_status,
                content_md=result["answer"],
                input_tokens=int(result.get("input_tokens", 0) or 0),
                output_tokens=int(result.get("output_tokens", 0) or 0),
                total_tokens=int(result.get("total_tokens", 0) or 0),
                run_duration_ms=elapsed_ms,
            )
            db.add(assistant_message)
            db.commit()
            db.refresh(assistant_message)
            _publish_messages(conversation.id, user_id, [_serialize_message(assistant_message)])
        conversation.pending_interrupt_id = None
        db.add(conversation)
        db.commit()

        return {
            "accepted": True,
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        err = _format_exception(exc)
        error_message = ConversationMessage(
            conversation_id=conversation.id,
            sender_user_id=None,
            sender_role="assistant",
            message_type="SystemMessage",
            message_status="failed",
            content_md=t("system.resume_failed", error=err),
            run_duration_ms=int((perf_counter() - started) * 1000),
        )
        db.add(error_message)
        db.commit()
        db.refresh(error_message)
        _publish_messages(conversation.id, user_id, [_serialize_message(error_message)])

        return {
            "accepted": False,
            "error": err,
            "message": _serialize_message(error_message),
        }


def _handle_user_message_work_item(item: WorkItem) -> None:
    execute_user_message_and_persist(
        conversation_id=item.conversation_id,
        user_id=item.user_id,
        content=str(item.payload.get("content") or ""),
    )


def _handle_interrupt_resume_work_item(item: WorkItem) -> None:
    execute_interrupt_resume_and_persist(
        conversation_id=item.conversation_id,
        interrupt_id=str(item.payload.get("interrupt_id") or ""),
        decision=str(item.payload.get("decision") or ""),
        user_id=item.user_id,
    )


def _handle_scheduled_task_work_item(item: WorkItem) -> None:
    task_id = str(item.payload.get("task_id") or "").strip()
    if not task_id:
        logger.warning("Scheduled task work item missing task_id: %s", item.id)
        return
    execute_scheduled_task_and_persist(task_id)


def _resolve_workspace_root() -> tuple[Path, str]:
    settings = get_settings()
    extra = settings.model_extra or {}
    docker_cfg = extra.get("docker", {}) or {}
    workspace_root = Path(str(docker_cfg.get("workspace_root", "./workspaces"))).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = (Path.cwd() / workspace_root).resolve()
    workdir_alias = docker_cfg.get("workdir", "/workspace") or "/workspace"
    workdir_alias = "/" + str(workdir_alias).strip("/")
    return workspace_root, workdir_alias


def _sanitize_filename(filename: str) -> tuple[str, str]:
    raw_name = Path(filename).name
    base, ext = os.path.splitext(raw_name)
    ext = ext.lower()
    # Allow UTF-8 names but block path separators/control chars.
    base = base.replace("/", "_").replace("\\", "_").replace("\x00", "_")
    base = re.sub(r"[\x00-\x1f\x7f]", "_", base)
    base = re.sub(r"[<>:\"|?*]", "_", base)
    base = re.sub(r"\s+", " ", base).strip()
    base = base.strip(".")
    if len(base) > 80:
        base = base[:80].rstrip()
    if not base:
        base = "upload"
    return base, ext


def _resolve_conversation_file_path(
    conversation_id: str,
    raw_path: str,
    *,
    required_prefix: str | None = None,
    required_suffix: str | None = None,
) -> tuple[Path, str]:
    raw = str(raw_path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path is required")

    workspace_root, workdir_alias = _resolve_workspace_root()
    workspace_dir = (workspace_root / conversation_id).resolve()
    alias_prefix = workdir_alias.rstrip("/") + "/"

    if raw == workdir_alias:
        rel = ""
    elif raw.startswith(alias_prefix):
        rel = raw[len(alias_prefix) :]
    elif raw.startswith("/"):
        raise HTTPException(status_code=400, detail="Absolute path is not allowed")
    else:
        rel = raw.lstrip("/")

    if not rel or ".." in Path(rel).parts:
        raise HTTPException(status_code=400, detail="Invalid path")

    if required_prefix and not rel.startswith(required_prefix):
        raise HTTPException(status_code=400, detail="Invalid path")

    target = (workspace_dir / rel).resolve()
    try:
        target.relative_to(workspace_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc

    if required_suffix and target.suffix.lower() != required_suffix.lower():
        raise HTTPException(status_code=400, detail=f"Only {required_suffix} files are allowed")

    return target, rel


def _merge_daemon_cfg(entry: dict, docker_cfg: dict) -> dict:
    merged = dict(entry)
    if "daemon_workspace_root" not in merged:
        if "workspace_root" in merged:
            merged["daemon_workspace_root"] = merged.get("workspace_root")
        elif docker_cfg.get("daemon_workspace_root"):
            merged["daemon_workspace_root"] = docker_cfg.get("daemon_workspace_root")
    if "daemon_host" not in merged and merged.get("host"):
        merged["daemon_host"] = merged.get("host")
    if "host" not in merged and merged.get("daemon_host"):
        merged["host"] = merged.get("daemon_host")
    if "tls" not in merged and docker_cfg.get("tls"):
        merged["tls"] = docker_cfg.get("tls")
    return merged


def _daemon_hosts_from_cfg(docker_cfg: dict) -> list[dict]:
    hosts = docker_cfg.get("daemon_hosts")
    if isinstance(hosts, list):
        return [h for h in hosts if isinstance(h, dict)]
    host = docker_cfg.get("daemon_host")
    if host:
        return [
            _merge_daemon_cfg(
                {"name": "default", "host": str(host), "workspace_root": docker_cfg.get("daemon_workspace_root")},
                docker_cfg,
            )
        ]
    return []


def _pick_daemon(docker_cfg: dict) -> dict | None:
    hosts = [h for h in _daemon_hosts_from_cfg(docker_cfg) if h.get("host")]
    if not hosts:
        return None
    choice = random.choice(hosts)
    return _merge_daemon_cfg(choice, docker_cfg)


def _lookup_daemon(docker_cfg: dict, host: str | None) -> dict | None:
    if not host:
        return None
    for entry in _daemon_hosts_from_cfg(docker_cfg):
        if str(entry.get("host")) == str(host):
            return _merge_daemon_cfg(entry, docker_cfg)
    return None


def _ensure_daemon_for_conversation(
    conversation: Conversation,
    docker_cfg: dict,
    db: Session,
) -> dict | None:
    daemon_cfg = _lookup_daemon(docker_cfg, conversation.daemon_host)
    if daemon_cfg:
        return daemon_cfg
    daemon_cfg = _pick_daemon(docker_cfg)
    if daemon_cfg and daemon_cfg.get("host"):
        conversation.daemon_host = str(daemon_cfg.get("host"))
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
    return daemon_cfg


@router.get("")
def list_conversations(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    conversations = db.scalars(
        select(Conversation)
        .where(Conversation.user_id == current_user.id)
        .order_by(desc(Conversation.is_pinned), desc(Conversation.pinned_at), desc(Conversation.updated_at))
    ).all()
    return {"items": [_serialize_conversation(c) for c in conversations]}


@router.post("")
def create_conversation(
    payload: CreateConversationRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    daemon_cfg = _pick_daemon(docker_cfg)
    conversation = Conversation(
        user_id=current_user.id,
        title=payload.title,
        model_name=payload.model,
        execution_backend=resolve_backend_name(current_user.id, payload.execution_backend),
        container_status="running",
        daemon_host=daemon_cfg.get("host") if daemon_cfg else None,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    try:
        backend = _backend_for_conversation(conversation)
        backend.set_conversation_daemon(conversation.id, daemon_cfg)
        deepagent_service.warm_conversation_context(conversation.id, current_user.id, db=db)
        backend.ensure_ready(conversation.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("prepare_conversation_skills failed: %s", exc)

    return {
        **_serialize_conversation(conversation),
    }


@router.post("/{conversation_id}/refresh_skills")
def refresh_conversation_skills(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    tool_names = _backend_for_conversation(conversation).prepare_conversation_skills(conversation_id, current_user.id, db=db)
    deepagent_service.warm_conversation_context(conversation_id, current_user.id, db=db)
    return {"refreshed": True, "skill_count": len(tool_names)}


@router.get("/{conversation_id}/workspace")
def get_conversation_workspace(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    install_rows = db.scalars(
        select(ConversationSkillInstall)
        .where(
            ConversationSkillInstall.owner_user_id == current_user.id,
            ConversationSkillInstall.conversation_id == conversation_id,
        )
        .order_by(desc(ConversationSkillInstall.updated_at))
    ).all()
    installed_skill_ids = [row.skill_id for row in install_rows]
    installed_skills = (
        db.scalars(select(Skill).where(Skill.id.in_(installed_skill_ids), Skill.status != "rejected")).all()
        if installed_skill_ids
        else []
    )
    install_by_skill_id = {row.skill_id: row for row in install_rows}
    setting_rows = db.scalars(
        select(ConversationSkillSetting).where(
            ConversationSkillSetting.owner_user_id == current_user.id,
            ConversationSkillSetting.conversation_id == conversation_id,
        )
    ).all()
    setting_by_skill_id = {row.skill_id: row for row in setting_rows}
    skill_rows = {row.id: row for row in installed_skills}
    user_scope_skills = db.scalars(
        select(Skill)
        .where(
            Skill.owner_user_id == current_user.id,
            Skill.status != "rejected",
            Skill.scope == "user",
            Skill.source_type != "agent",
        )
        .order_by(desc(Skill.updated_at))
        .limit(24)
    ).all()
    for row in user_scope_skills:
        setting = setting_by_skill_id.get(row.id)
        if setting is None or setting.enabled:
            skill_rows[row.id] = row
    conversation_scope_skills = db.scalars(
        select(Skill)
        .where(
            Skill.owner_user_id == current_user.id,
            Skill.status != "rejected",
            Skill.scope == "conversation",
            Skill.conversation_id == conversation_id,
        )
        .order_by(desc(Skill.updated_at))
        .limit(12)
    ).all()
    for row in conversation_scope_skills:
        skill_rows[row.id] = row
    ordered_skill_rows = sorted(
        skill_rows.values(),
        key=lambda item: getattr(install_by_skill_id.get(item.id), "updated_at", None) or item.updated_at,
        reverse=True,
    )[:12]

    user_connection_rows = db.scalars(
        select(MCPConnection)
        .where(MCPConnection.owner_user_id == current_user.id)
        .order_by(desc(MCPConnection.updated_at))
        .limit(50)
    ).all()
    binding_rows = [item for item in user_connection_rows if item.enabled]

    task_rows = db.scalars(
        select(ScheduledTask)
        .where(
            ScheduledTask.owner_user_id == current_user.id,
            ScheduledTask.conversation_id == conversation_id,
        )
        .order_by(desc(ScheduledTask.created_at))
        .limit(12)
    ).all()

    backend_runs = db.scalars(
        select(BackendRunLog)
        .where(
            BackendRunLog.conversation_id == conversation_id,
            BackendRunLog.user_id == current_user.id,
        )
        .order_by(desc(BackendRunLog.created_at))
        .limit(20)
    ).all()

    message_rows = db.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(desc(ConversationMessage.created_at))
    ).all()
    total_tokens = sum(int(item.total_tokens or 0) for item in message_rows)
    total_runtime_ms = sum(int(item.run_duration_ms or 0) for item in message_rows)
    attachments_count = sum(len(_extract_attachments(item.attachments_json)) for item in message_rows)
    last_message_at = message_rows[0].created_at.isoformat() if message_rows and message_rows[0].created_at else None

    runtime_state = runtime_registry.snapshot(conversation_id)

    return {
        "conversation": _serialize_conversation(conversation),
        "stats": {
            "message_count": len(message_rows),
            "attachments_count": attachments_count,
            "total_tokens": total_tokens,
            "total_runtime_ms": total_runtime_ms,
            "last_message_at": last_message_at,
        },
        "skills": [
            _serialize_skill_chip(
                item,
                install_by_skill_id.get(item.id),
                conversation_enabled=(setting_by_skill_id.get(item.id).enabled if setting_by_skill_id.get(item.id) else True),
            )
            for item in ordered_skill_rows
        ],
        "my_skills": [
            _serialize_skill_chip(
                item,
                install_by_skill_id.get(item.id),
                conversation_enabled=(setting_by_skill_id.get(item.id).enabled if setting_by_skill_id.get(item.id) else True),
            )
            for item in user_scope_skills
        ],
        "mcp": {
            "bindings": [_serialize_workspace_binding_summary(item) for item in binding_rows],
            "all_connections": [_serialize_connection_summary(item) for item in user_connection_rows],
            "capabilities": list_connection_capabilities(db, conversation_id, current_user.id),
        },
        "tasks": {
            "runtime": {
                "active": bool(runtime_state.active),
                "current_work_item_id": runtime_state.current_work_item_id,
                "pending_count": int(runtime_state.pending_count or 0),
                "retry_count": int(runtime_state.retry_count or 0),
                "last_active_at": runtime_state.last_active_at.isoformat() if runtime_state.last_active_at else None,
            },
            "runs": [_serialize_backend_run(item) for item in backend_runs],
            "scheduled": [_serialize_task_summary(item) for item in task_rows],
        },
    }


@router.patch("/{conversation_id}")
def update_conversation(
    conversation_id: str,
    payload: UpdateConversationRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    settings = get_settings()
    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    daemon_cfg = _ensure_daemon_for_conversation(conversation, docker_cfg, db)
    _backend_for_conversation(conversation).set_conversation_daemon(conversation_id, daemon_cfg)

    if payload.title is None and payload.is_pinned is None and payload.execution_backend is None:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="title cannot be empty")
        conversation.title = title

    if payload.is_pinned is not None:
        conversation.is_pinned = bool(payload.is_pinned)
        conversation.pinned_at = datetime.now(timezone.utc) if conversation.is_pinned else None

    if payload.execution_backend is not None:
        conversation.execution_backend = normalize_backend_name(payload.execution_backend)

    db.commit()
    db.refresh(conversation)
    try:
        backend = _backend_for_conversation(conversation)
        backend.set_conversation_daemon(conversation_id, daemon_cfg)
        backend.prepare_conversation_skills(conversation_id, current_user.id, db=db)
        backend.ensure_ready(conversation_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("update_conversation backend sync failed: %s", exc)
    return _serialize_conversation(conversation)


@router.delete("/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    settings = get_settings()
    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    daemon_cfg = _ensure_daemon_for_conversation(conversation, docker_cfg, db)
    backend = _backend_for_conversation(conversation)
    backend.set_conversation_daemon(conversation_id, daemon_cfg)
    threading.Thread(
        target=backend.shutdown,
        args=(conversation_id,),
        daemon=True,
    ).start()

    db.query(ConversationSkillInstall).filter(ConversationSkillInstall.conversation_id == conversation_id).delete()
    db.query(ConversationSkillSetting).filter(ConversationSkillSetting.conversation_id == conversation_id).delete()
    db.delete(conversation)
    db.commit()
    workspace_deleted = False
    workspace_error = None
    skills_deleted = False
    skills_error = None
    try:
        workspace_root, _ = _resolve_workspace_root()
        workspace_dir = (workspace_root / conversation_id).resolve()
        if workspace_dir.exists() and str(workspace_dir).startswith(str(workspace_root.resolve())):
            shutil.rmtree(workspace_dir)
        workspace_deleted = True
    except Exception as exc:  # noqa: BLE001
        workspace_error = _format_exception(exc)
    try:
        skills_root = Path(get_settings().skill_storage.conversationskills_dir).expanduser()
        if not skills_root.is_absolute():
            skills_root = (Path.cwd() / skills_root).resolve()
        skills_dir = (skills_root / conversation_id).resolve()
        if skills_dir.exists() and str(skills_dir).startswith(str(skills_root)):
            shutil.rmtree(skills_dir)
        skills_deleted = True
    except Exception as exc:  # noqa: BLE001
        skills_error = _format_exception(exc)

    return {
        "deleted": True,
        "id": conversation_id,
        "workspace_deleted": workspace_deleted,
        "workspace_error": workspace_error,
        "skills_deleted": skills_deleted,
        "skills_error": skills_error,
    }


@router.get("/{conversation_id}/messages")
def get_messages(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    settings = get_settings()
    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    daemon_cfg = _ensure_daemon_for_conversation(conversation, docker_cfg, db)
    backend = _backend_for_conversation(conversation)
    backend.set_conversation_daemon(conversation_id, daemon_cfg)
    try:
        backend.ensure_ready(conversation_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_conversation_ready failed: %s", exc)

    messages = db.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.created_at.asc())
    ).all()
    return {"items": [_serialize_message(m) for m in messages]}


@router.post("/{conversation_id}/messages")
def send_message(
    conversation_id: str,
    payload: SendMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    settings = get_settings()
    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    daemon_cfg = _ensure_daemon_for_conversation(conversation, docker_cfg, db)
    _backend_for_conversation(conversation).set_conversation_daemon(conversation_id, daemon_cfg)

    human_message = ConversationMessage(
        conversation_id=conversation_id,
        sender_user_id=current_user.id,
        sender_role="human",
        message_type="human_text",
        message_status="done",
        content_md=payload.content,
    )
    db.add(human_message)
    db.commit()
    db.refresh(human_message)
    _publish_messages(conversation_id, current_user.id, [_serialize_message(human_message)])

    pending_schedule = getattr(conversation, "pending_schedule_json", None) or None
    if pending_schedule:
        interval_request = _parse_interval_only(payload.content)
        if interval_request:
            task = _create_scheduled_task(
                db,
                current_user=current_user,
                conversation_id=conversation_id,
                task_payload={
                    "title": pending_schedule.get("title") or _build_scheduled_task_title(pending_schedule.get("prompt") or payload.content),
                    "prompt": pending_schedule.get("prompt") or payload.content,
                    "schedule_type": interval_request["schedule_type"],
                    "schedule_value": interval_request["schedule_value"],
                    "run_at": interval_request["run_at"],
                },
            )
            conversation.pending_schedule_json = None
            db.add(conversation)
            db.commit()
            _append_system_message(
                db,
                conversation_id=conversation_id,
                user_id=current_user.id,
                content=(
                    f"已创建定时任务：{task.title}\n"
                    f"发送频率：{interval_request['interval_label']}\n"
                    f"计划执行时间：{task.next_run.isoformat() if task.next_run else '-'}"
                ),
            )
            return {
                "accepted": True,
                "queued": False,
                "requires_interrupt_decision": False,
                "scheduled_task": {
                    "id": task.id,
                    "title": task.title,
                    "next_run": task.next_run.isoformat() if task.next_run else None,
                },
            }

    reminder_request = _parse_reminder_request(payload.content)
    if reminder_request:
        task = ScheduledTask(
            owner_user_id=current_user.id,
            conversation_id=conversation_id,
            title=reminder_request["title"],
            prompt=reminder_request["prompt"],
            schedule_type=reminder_request["schedule_type"],
            schedule_value=reminder_request["schedule_value"],
            status="active",
            next_run=reminder_request["run_at"],
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        conversation.pending_schedule_json = None
        db.add(conversation)
        db.commit()

        confirmation_message = ConversationMessage(
            conversation_id=conversation_id,
            sender_user_id=None,
            sender_role="assistant",
            message_type="SystemMessage",
            message_status="done",
            content_md=(
                f"\u5df2\u521b\u5efa\u5b9a\u65f6\u63d0\u9192\uff1a{task.title}\n"
                f"\u8ba1\u5212\u6267\u884c\u65f6\u95f4\uff1a{task.next_run.isoformat() if task.next_run else '-'}"
            ),
        )
        db.add(confirmation_message)
        db.commit()
        db.refresh(confirmation_message)
        _publish_messages(conversation_id, current_user.id, [_serialize_message(confirmation_message)])
        return {
            "accepted": True,
            "queued": False,
            "requires_interrupt_decision": False,
            "scheduled_task": {
                "id": task.id,
                "title": task.title,
                "next_run": task.next_run.isoformat() if task.next_run else None,
            },
        }

        confirmation_message = ConversationMessage(
            conversation_id=conversation_id,
            sender_user_id=None,
            sender_role="assistant",
            message_type="SystemMessage",
            message_status="done",
            content_md=(
                f"已创建定时提醒：{task.title}\n"
                f"计划执行时间：{task.next_run.isoformat() if task.next_run else '-'}"
            ),
        )
        db.add(confirmation_message)
        db.commit()
        db.refresh(confirmation_message)
        _publish_messages(conversation_id, current_user.id, [_serialize_message(confirmation_message)])
        return {
            "accepted": True,
            "queued": False,
            "requires_interrupt_decision": False,
            "scheduled_task": {
                "id": task.id,
                "title": task.title,
                "next_run": task.next_run.isoformat() if task.next_run else None,
            },
        }

    pending_prompt = _parse_pending_schedule_prompt(payload.content)
    if pending_prompt:
        conversation.pending_schedule_json = pending_prompt
        db.add(conversation)
        db.commit()
        queue_manager.enqueue(
            WorkItem(
                kind="user_message",
                conversation_id=conversation_id,
                user_id=current_user.id,
                payload={
                    "content": (
                        f"{payload.content}\n\n"
                        "[System Note]\n"
                        "The user intends to create a scheduled recurring task, but the interval is missing. "
                        "Ask one short follow-up question only: how often should it be sent. "
                        "Give 2-3 concrete examples such as every 30 minutes, every 2 hours, or once a day. "
                        "Do not answer the original task yet."
                    )
                },
            )
        )
        return {
            "accepted": True,
            "queued": True,
            "requires_interrupt_decision": False,
            "awaiting_schedule_interval": True,
        }

    queue_manager.enqueue(
        WorkItem(
            kind="user_message",
            conversation_id=conversation_id,
            user_id=current_user.id,
            payload={"content": payload.content},
        )
    )
    return {
        "accepted": True,
        "queued": True,
        "requires_interrupt_decision": False,
    }


@router.post("/{conversation_id}/interrupts/{interrupt_id}/decision")
def decide_interrupt(
    conversation_id: str,
    interrupt_id: str,
    payload: InterruptDecisionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    settings = get_settings()
    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    daemon_cfg = _ensure_daemon_for_conversation(conversation, docker_cfg, db)
    _backend_for_conversation(conversation).set_conversation_daemon(conversation_id, daemon_cfg)

    decision_message = ConversationMessage(
        conversation_id=conversation_id,
        sender_user_id=current_user.id,
        sender_role="human",
        message_type="human_interrupt_decision",
        message_status="done",
        content_md=payload.decision,
    )
    db.add(decision_message)
    db.commit()
    db.refresh(decision_message)
    _publish_messages(conversation_id, current_user.id, [_serialize_message(decision_message)])

    if payload.decision == "allow_all":
        conversation.pending_interrupt_id = None
        db.add(conversation)
        db.commit()
        auto_message = ConversationMessage(
            conversation_id=conversation_id,
            sender_user_id=None,
            sender_role="assistant",
            message_type="SystemMessage",
            message_status="pending",
            content_md=t("system.allow_all_notice"),
        )
        db.add(auto_message)
        db.commit()
        db.refresh(auto_message)
        _publish_messages(conversation_id, current_user.id, [_serialize_message(auto_message)])
        queue_manager.enqueue(
            WorkItem(
                kind="interrupt_resume",
                conversation_id=conversation_id,
                user_id=current_user.id,
                payload={
                    "interrupt_id": interrupt_id,
                    "decision": payload.decision,
                },
            )
        )
        return {
            "accepted": True,
            "queued": True,
        }

    queue_manager.enqueue(
        WorkItem(
            kind="interrupt_resume",
            conversation_id=conversation_id,
            user_id=current_user.id,
            payload={
                "interrupt_id": interrupt_id,
                "decision": payload.decision,
            },
        )
    )
    return {
        "accepted": True,
        "queued": True,
    }


@router.get("/{conversation_id}/interrupts/pending")
def get_pending_interrupt(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"interrupt_id": conversation.pending_interrupt_id}


@router.get("/{conversation_id}/events")
def stream_conversation_events(
    conversation_id: str,
    token: str | None = None,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    current_user = _user_from_token(token, db)
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    settings = get_settings()
    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    daemon_cfg = _ensure_daemon_for_conversation(conversation, docker_cfg, db)
    backend = _backend_for_conversation(conversation)
    backend.set_conversation_daemon(conversation_id, daemon_cfg)
    try:
        backend.ensure_ready(conversation_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_conversation_ready failed: %s", exc)

    try:
        subscriber_id, q = stream_event_publisher.subscribe(conversation_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    def event_stream():
        try:
            connected_payload = {
                "event_type": "system.connected",
                "payload": {"conversation_id": conversation_id},
                "ts": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            }
            yield f"event: system.connected\ndata: {json.dumps(connected_payload, ensure_ascii=False)}\n\n"
            while True:
                if stream_event_publisher.is_shutdown():
                    break
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                if item.get("event_type") == "system.shutdown":
                    break
                if item.get("user_id") != current_user.id:
                    continue
                data = {
                    "event_id": item.get("event_id"),
                    "event_type": item.get("event_type"),
                    "payload": item.get("payload"),
                    "ts": item.get("ts"),
                }
                event_type = str(item.get("event_type") or "message")
                yield f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            stream_event_publisher.unsubscribe(conversation_id, subscriber_id)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


@router.post("/{conversation_id}/debug/exec")
def debug_exec(
    conversation_id: str,
    payload: DebugExecRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    settings = get_settings()
    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    daemon_cfg = _ensure_daemon_for_conversation(conversation, docker_cfg, db)
    backend = _backend_for_conversation(conversation)
    backend.set_conversation_daemon(conversation_id, daemon_cfg)
    allowed, reason = can_use_debug_exec(current_user, settings)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason or "debug_exec is not allowed")

    return backend.debug_exec(conversation_id=conversation_id, command=payload.command)


@router.post("/{conversation_id}/attachments")
def upload_attachment(
    conversation_id: str,
    file: UploadFile = File(...),
    convert_to_markdown: bool = Form(True),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")

    base, ext = _sanitize_filename(file.filename)
    if ext not in ALLOWED_OFFICE_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_OFFICE_EXTENSIONS))
        raise HTTPException(status_code=400, detail=f"Only office files are supported: {allowed}")

    workspace_root, workdir_alias = _resolve_workspace_root()
    workspace_dir = (workspace_root / conversation_id / "uploads").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    unique = uuid4().hex[:8]
    saved_name = f"{base}_{unique}{ext}"
    target_path = (workspace_dir / saved_name).resolve()
    if not str(target_path).startswith(str(workspace_dir)):
        raise HTTPException(status_code=400, detail="Invalid upload path")

    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    max_bytes = 20 * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(status_code=400, detail="File too large (max 20MB)")

    target_path.write_bytes(data)

    markdown_info = None
    if convert_to_markdown:
        md_name = f"{base}_{unique}.md"
        md_path = (workspace_dir / md_name).resolve()
        try:
            result = extract_office_to_markdown(target_path)
            md_path.write_text(result.markdown, encoding="utf-8")
            markdown_info = {
                "source_type": result.source_type,
                "workspace_path": f"{workdir_alias}/uploads/{md_name}",
                "char_count": len(result.markdown),
                "warnings": result.warnings,
            }
        except Exception as exc:  # noqa: BLE001
            markdown_info = {
                "source_type": "unknown",
                "error": _format_exception(exc),
            }

    attachment = {
        "original_name": file.filename,
        "saved_name": saved_name,
        "content_type": file.content_type,
        "size_bytes": len(data),
        "workspace_path": f"{workdir_alias}/uploads/{saved_name}",
        "markdown": markdown_info,
    }

    message = ConversationMessage(
        conversation_id=conversation_id,
        sender_user_id=current_user.id,
        sender_role="user",
        message_type="human_attachment",
        message_status="done",
        content_md=t("system.attachment_uploaded", filename=file.filename),
        attachments_json={"items": [attachment]},
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    serialized = _serialize_message(message)
    _publish_messages(conversation_id, current_user.id, [serialized])

    return {"message": serialized, "attachment": attachment}


@router.get("/{conversation_id}/attachments")
def list_attachments(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = db.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .where(ConversationMessage.attachments_json.is_not(None))
        .order_by(desc(ConversationMessage.created_at))
    ).all()

    items: list[dict] = []
    for msg in messages:
        created_at = msg.created_at
        if created_at is None:
            created_at = datetime.now(timezone.utc)
        for att in _extract_attachments(msg.attachments_json):
            entry = dict(att)
            entry["uploaded_at"] = created_at.isoformat()
            entry["message_id"] = msg.id
            items.append(entry)

    return {"items": items}


@router.get("/{conversation_id}/attachments/download")
def download_attachment(
    conversation_id: str,
    path: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    target, _rel = _resolve_conversation_file_path(
        conversation_id,
        path,
        required_prefix="uploads/",
    )
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(path=str(target), filename=target.name, media_type="application/octet-stream")


@router.get("/{conversation_id}/attachments/markdown")
def get_attachment_markdown(
    conversation_id: str,
    path: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    target, rel = _resolve_conversation_file_path(
        conversation_id,
        path,
        required_prefix="uploads/",
        required_suffix=".md",
    )
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    data = target.read_bytes()
    max_bytes = 500_000
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    content = data.decode("utf-8", errors="replace")

    return {"path": rel, "content": content, "truncated": truncated}


register_handler("user_message", _handle_user_message_work_item)
register_handler("interrupt_resume", _handle_interrupt_resume_work_item)
register_handler("scheduled_task", _handle_scheduled_task_work_item)
