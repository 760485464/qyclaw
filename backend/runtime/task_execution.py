from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from time import perf_counter

from sqlalchemy.orm import Session

from backend.agent_backends import get_backend
from backend.core.config import get_settings
from backend.core.database import SessionLocal
from backend.core.models import (
    BackendRunLog,
    Conversation,
    MCPConnection,
    ConversationMessage,
    ScheduledTask,
    ScheduledTaskRunLog,
    Skill,
)
from backend.i18n import t
from backend.runtime.backend_router import resolve_backend_for_conversation, resolve_fallback_backend_name
from backend.runtime.memory_manager import (
    update_conversation_memory,
)
from backend.services.deepagents.service import SYSTEM_TOOL_NAMES, deepagent_service
from backend.services.hindsight_service import hindsight_service
from backend.services.stream_events import stream_event_publisher

logger = logging.getLogger(__name__)


def _compose_effective_content(
    *,
    content: str,
    memory_context: str,
) -> str:
    sections: list[str] = []
    if memory_context:
        sections.append(memory_context)
    sections.append("[Current User Message]\n" + content)
    return "\n\n".join(part for part in sections if str(part or "").strip())


def _compute_next_cron_run(expression: str, now: datetime | None = None) -> datetime | None:
    parts = str(expression or "").split()
    if len(parts) != 5:
        return None
    minute, hour, day, month, weekday = parts
    current = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 370):
        if not _cron_field_matches(current.minute, minute, 0, 59):
            current += timedelta(minutes=1)
            continue
        if not _cron_field_matches(current.hour, hour, 0, 23):
            current += timedelta(minutes=1)
            continue
        if not _cron_field_matches(current.day, day, 1, 31):
            current += timedelta(minutes=1)
            continue
        if not _cron_field_matches(current.month, month, 1, 12):
            current += timedelta(minutes=1)
            continue
        cron_weekday = (current.weekday() + 1) % 7
        if not _cron_field_matches(cron_weekday, weekday, 0, 6):
            current += timedelta(minutes=1)
            continue
        return current
    return None


def _cron_field_matches(value: int, field: str, minimum: int, maximum: int) -> bool:
    token = str(field or "").strip()
    if token == "*":
        return True
    if token.startswith("*/"):
        try:
            step = int(token[2:])
        except ValueError:
            return False
        return step > 0 and value % step == 0
    try:
        numeric = int(token)
    except ValueError:
        return False
    return minimum <= numeric <= maximum and value == numeric


def _format_exception(exc: Exception) -> str:
    msg = str(exc).strip()
    exc_name = exc.__class__.__name__
    return f"{exc_name}: {msg}" if msg else exc_name


def _serialize_message(m: ConversationMessage) -> dict:
    created_at = m.created_at or datetime.now(timezone.utc)
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


def _publish_messages(conversation_id: str, user_id: str, messages: list[dict]) -> None:
    for msg in messages:
        stream_event_publisher.publish_conversation_event(
            conversation_id=conversation_id,
            user_id=user_id,
            event_type="message.created",
            payload=msg,
        )


def _publish_message_event(
    conversation_id: str,
    user_id: str,
    message: dict,
    event_type: str,
) -> None:
    stream_event_publisher.publish_conversation_event(
        conversation_id=conversation_id,
        user_id=user_id,
        event_type=event_type,
        payload=message,
    )


def _publish_runtime_stage(
    conversation_id: str,
    user_id: str,
    stage: str,
    *,
    backend: str | None = None,
    work_kind: str | None = None,
) -> None:
    stream_event_publisher.publish_conversation_event(
        conversation_id=conversation_id,
        user_id=user_id,
        event_type="runtime.stage",
        payload={
            "stage": stage,
            "backend": backend,
            "work_kind": work_kind,
        },
    )


def _is_reminder_task(task: ScheduledTask) -> bool:
    title = str(task.title or "").strip().lower()
    prompt = str(task.prompt or "").strip().lower()
    reminder_tokens = ("reminder:", "提醒", "remind ")
    return any(title.startswith(token) for token in reminder_tokens) or any(prompt.startswith(token) for token in reminder_tokens)


def _deliver_scheduled_reminder(
    db: Session,
    *,
    task: ScheduledTask,
    conversation: Conversation,
) -> dict:
    prompt = str(task.prompt or "").strip() or str(task.title or "").strip() or "Reminder"
    reminder_message = ConversationMessage(
        conversation_id=conversation.id,
        sender_user_id=None,
        sender_role="assistant",
        message_type="SystemMessage",
        message_status="done",
        content_md=f"提醒：{prompt}",
        run_duration_ms=0,
    )
    db.add(reminder_message)
    db.commit()
    db.refresh(reminder_message)
    _publish_messages(conversation.id, task.owner_user_id, [_serialize_message(reminder_message)])
    return {"accepted": True, "message": _serialize_message(reminder_message)}


def _make_progress_handler(
    conversation_id: str,
    user_id: str,
    db: Session,
    started: float,
    *,
    skill_tool_names: set[str] | None = None,
    has_mcp_context: bool = False,
):
    state: dict[str, object] = {
        "ai_message": None,
        "ai_buffer": "",
        "last_flush": perf_counter(),
        "trigger_tags": set(),
    }

    skill_tool_names = {str(item) for item in (skill_tool_names or set()) if str(item).strip()}

    def classify_trigger_tags(tool_name: str) -> list[str]:
        normalized = str(tool_name or "").strip()
        if not normalized:
            return ["mcp"] if has_mcp_context else []
        tags: list[str] = []
        if normalized in skill_tool_names:
            tags.append("skill")
        elif normalized not in SYSTEM_TOOL_NAMES and has_mcp_context:
            tags.append("mcp")
        return tags

    def flush_ai(
        force: bool = False,
        final_status: str | None = None,
        tokens: dict[str, int] | None = None,
        run_duration_ms: int | None = None,
    ):
        ai_message: ConversationMessage | None = state["ai_message"]  # type: ignore[assignment]
        buffer = str(state["ai_buffer"] or "")
        trigger_tags = sorted(str(item) for item in (state["trigger_tags"] or set()))
        now = perf_counter()
        should_flush = force or len(buffer) >= 40 or (now - float(state["last_flush"])) >= 0.15
        if not ai_message and not buffer:
            return None
        if not ai_message:
            if not buffer and not force:
                return None
            ai_message = ConversationMessage(
                conversation_id=conversation_id,
                sender_user_id=None,
                sender_role="assistant",
                message_type="AIMessage",
                message_status="streaming",
                content_md=buffer,
                attachments_json={"trigger_tags": trigger_tags} if trigger_tags else None,
                run_duration_ms=int((perf_counter() - started) * 1000),
            )
            db.add(ai_message)
            db.commit()
            db.refresh(ai_message)
            _publish_message_event(
                conversation_id,
                user_id,
                _serialize_message(ai_message),
                "message.created",
            )
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
        ai_message.attachments_json = {"trigger_tags": trigger_tags} if trigger_tags else None
        db.add(ai_message)
        db.commit()
        db.refresh(ai_message)
        _publish_message_event(
            conversation_id,
            user_id,
            _serialize_message(ai_message),
            "message.updated",
        )
        state["last_flush"] = now
        return ai_message

    def on_progress(event: dict) -> None:
        event_type = event.get("type")
        if event_type == "tool_output":
            tool_content = str(event.get("content") or "")
            tool_name = str(event.get("tool_name") or "") or None
            trigger_tags = classify_trigger_tags(tool_name or "")
            state["trigger_tags"] = set(state["trigger_tags"] or set()).union(trigger_tags)
            tool_message = ConversationMessage(
                conversation_id=conversation_id,
                sender_user_id=None,
                sender_role="assistant",
                message_type="ToolMessage",
                tool_name=tool_name,
                message_status="failed"
                if "Command failed with exit code" in tool_content or "Error executing" in tool_content
                else "done",
                content_md=tool_content,
                attachments_json={"trigger_tags": trigger_tags} if trigger_tags else None,
                run_duration_ms=int((perf_counter() - started) * 1000),
            )
            db.add(tool_message)
            db.commit()
            db.refresh(tool_message)
            _publish_message_event(
                conversation_id,
                user_id,
                _serialize_message(tool_message),
                "message.created",
            )
            return

        if event_type == "ai_chunk":
            chunk = str(event.get("content") or "")
            if not chunk:
                return
            state["ai_buffer"] = str(state["ai_buffer"]) + chunk
            flush_ai(force=False)

    return on_progress, flush_ai


def _ensure_conversation_daemon(conversation: Conversation) -> None:
    _backend_name, backend = resolve_backend_for_conversation(conversation)
    settings = get_settings()
    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    daemon_cfg = None
    daemon_host = getattr(conversation, "daemon_host", None)
    hosts_cfg = docker_cfg.get("daemon_hosts")
    if daemon_host and isinstance(hosts_cfg, list):
        for entry in hosts_cfg:
            if isinstance(entry, dict) and str(entry.get("host")) == str(daemon_host):
                daemon_cfg = entry
                break
    if daemon_cfg is None and daemon_host:
        daemon_cfg = {"host": daemon_host}
    if daemon_cfg is None and docker_cfg.get("daemon_host"):
        daemon_cfg = {"host": docker_cfg.get("daemon_host")}
    backend.set_conversation_daemon(conversation.id, daemon_cfg)


def _record_backend_run(
    db: Session,
    *,
    conversation_id: str,
    user_id: str | None,
    work_kind: str,
    requested_backend: str,
    actual_backend: str,
    status: str,
    latency_ms: int,
    error: str | None = None,
    fallback_backend: str | None = None,
) -> None:
    db.add(
        BackendRunLog(
            conversation_id=conversation_id,
            user_id=user_id,
            work_kind=work_kind,
            requested_backend=requested_backend,
            actual_backend=actual_backend,
            fallback_backend=fallback_backend,
            status=status,
            latency_ms=latency_ms,
            error=error,
        )
    )
    db.commit()


def execute_user_message_and_persist(
    conversation_id: str,
    user_id: str,
    content: str,
    work_kind: str = "user_message",
) -> dict:
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        if not conversation or conversation.user_id != user_id:
            return {"accepted": False, "error": "Conversation not found"}

        request_started = perf_counter()
        stage_marks: dict[str, int] = {}

        def mark_stage(name: str) -> None:
            stage_marks[name] = int((perf_counter() - request_started) * 1000)

        requested_backend, backend = resolve_backend_for_conversation(conversation)
        _publish_runtime_stage(
            conversation_id,
            user_id,
            "preparing_context",
            backend=requested_backend,
            work_kind=work_kind,
        )
        daemon_started = perf_counter()
        _ensure_conversation_daemon(conversation)
        logger.info(
            "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=daemon_ready elapsed_ms=%s",
            conversation_id,
            user_id,
            requested_backend,
            work_kind,
            int((perf_counter() - daemon_started) * 1000),
        )
        mark_stage("daemon_ready")
        context_started = perf_counter()
        context_snapshot = deepagent_service.conversation_context_snapshot(conversation_id)
        if context_snapshot.get("has_mcp_context") is None:
            context_snapshot = deepagent_service.warm_conversation_context(conversation_id, user_id, db=db)
        logger.info(
            "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=conversation_context_ready elapsed_ms=%s agent_cached=%s has_mcp_context=%s skill_count=%s",
            conversation_id,
            user_id,
            requested_backend,
            work_kind,
            int((perf_counter() - context_started) * 1000),
            bool(context_snapshot.get("agent_cached")),
            bool(context_snapshot.get("has_mcp_context")),
            len(context_snapshot.get("skill_tool_names") or []),
        )
        cached_skill_tool_names = {
            str(name)
            for name in (context_snapshot.get("skill_tool_names") or [])
            if str(name or "").strip()
        }
        cached_has_mcp_context = bool(context_snapshot.get("has_mcp_context"))
        mark_stage("conversation_context_ready")
        _publish_runtime_stage(
            conversation_id,
            user_id,
            "recalling_memory",
            backend=requested_backend,
            work_kind=work_kind,
        )
        memory_recall_started = perf_counter()
        memory_context = hindsight_service.recall_for_turn(
            user_id=user_id,
            conversation_id=conversation_id,
            query=content,
        )
        logger.info(
            "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=memory_recall_done elapsed_ms=%s",
            conversation_id,
            user_id,
            requested_backend,
            work_kind,
            int((perf_counter() - memory_recall_started) * 1000),
        )
        mark_stage("memory_ready")
        effective_content = _compose_effective_content(
            content=content,
            memory_context=memory_context,
        )
        mark_stage("skills_ready")
        mark_stage("mcp_ready")
        _publish_runtime_stage(
            conversation_id,
            user_id,
            "generating_reply",
            backend=requested_backend,
            work_kind=work_kind,
        )
        started = perf_counter()
        on_progress, flush_ai = _make_progress_handler(
            conversation_id,
            user_id,
            db,
            started,
            skill_tool_names=cached_skill_tool_names,
            has_mcp_context=cached_has_mcp_context,
        )
        first_progress_ms: int | None = None
        first_ai_chunk_ms: int | None = None

        def instrumented_on_progress(event: dict[str, object]) -> None:
            nonlocal first_progress_ms, first_ai_chunk_ms
            if first_progress_ms is None:
                first_progress_ms = int((perf_counter() - request_started) * 1000)
                logger.info(
                    "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=first_progress elapsed_ms=%s event_type=%s",
                    conversation_id,
                    user_id,
                    requested_backend,
                    work_kind,
                    first_progress_ms,
                    event.get("type"),
                )
            if event.get("type") == "ai_chunk" and first_ai_chunk_ms is None:
                first_ai_chunk_ms = int((perf_counter() - request_started) * 1000)
                logger.info(
                    "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=first_ai_chunk elapsed_ms=%s",
                    conversation_id,
                    user_id,
                    requested_backend,
                    work_kind,
                    first_ai_chunk_ms,
                )
            on_progress(event)

        logger.info(
            "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=pre_backend_breakdown daemon_ms=%s context_ms=%s memory_ms=%s skills_ms=%s mcp_ms=%s pre_backend_ms=%s",
            conversation_id,
            user_id,
            requested_backend,
            work_kind,
            stage_marks.get("daemon_ready", 0),
            max(0, stage_marks.get("conversation_context_ready", 0) - stage_marks.get("daemon_ready", 0)),
            max(0, stage_marks.get("memory_ready", 0) - stage_marks.get("conversation_context_ready", 0)),
            max(0, stage_marks.get("skills_ready", 0) - stage_marks.get("memory_ready", 0)),
            max(0, stage_marks.get("mcp_ready", 0) - stage_marks.get("skills_ready", 0)),
            stage_marks.get("mcp_ready", 0),
        )

        try:
            result = backend.run_turn(
                conversation_id=conversation_id,
                content=effective_content,
                on_progress=instrumented_on_progress,
            )
            elapsed_ms = int((perf_counter() - started) * 1000)
            total_elapsed_ms = int((perf_counter() - request_started) * 1000)
            logger.info(
                "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=backend_done backend_elapsed_ms=%s total_elapsed_ms=%s first_progress_ms=%s first_ai_chunk_ms=%s interrupted=%s",
                conversation_id,
                user_id,
                requested_backend,
                work_kind,
                elapsed_ms,
                total_elapsed_ms,
                first_progress_ms,
                first_ai_chunk_ms,
                bool(result.get("interrupted")),
            )

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
                    conversation_id=conversation_id,
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
                _publish_messages(conversation_id, user_id, [_serialize_message(interrupt_message)])
                update_conversation_memory(
                    db,
                    conversation_id=conversation_id,
                    user_content=content,
                    assistant_content=interrupt_text,
                    message_id=interrupt_message.id,
                    updated_from="interrupt",
                    actor_user_id=user_id,
                )
                retain_started = perf_counter()
                hindsight_service.retain_turn(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    user_content=content,
                    assistant_content=interrupt_text,
                    tool_outputs=result.get("tool_outputs"),
                )
                logger.info(
                    "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=memory_retain_done elapsed_ms=%s updated_from=interrupt",
                    conversation_id,
                    user_id,
                    requested_backend,
                    work_kind,
                    int((perf_counter() - retain_started) * 1000),
                )
                _record_backend_run(
                    db,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    work_kind=work_kind,
                    requested_backend=requested_backend,
                    actual_backend=requested_backend,
                    fallback_backend=None,
                    status="interrupted",
                    latency_ms=elapsed_ms,
                )
                return {
                    "accepted": True,
                    "requires_interrupt_decision": True,
                    "interrupt_id": result["interrupt_id"],
                }

            assistant_text = str(result.get("answer") or "")
            assistant_message_id: str | None = None
            ai_message = flush_ai(
                force=True,
                final_status="done",
                tokens={
                    "input_tokens": int(result.get("input_tokens", 0) or 0),
                    "output_tokens": int(result.get("output_tokens", 0) or 0),
                    "total_tokens": int(result.get("total_tokens", 0) or 0),
                },
                run_duration_ms=elapsed_ms,
            )
            if not ai_message:
                assistant_message = ConversationMessage(
                    conversation_id=conversation_id,
                    sender_user_id=None,
                    sender_role="assistant",
                    message_type="AIMessage",
                    message_status="done",
                    content_md=result["answer"],
                    input_tokens=int(result.get("input_tokens", 0) or 0),
                    output_tokens=int(result.get("output_tokens", 0) or 0),
                    total_tokens=int(result.get("total_tokens", 0) or 0),
                    run_duration_ms=elapsed_ms,
                )
                db.add(assistant_message)
                db.commit()
                db.refresh(assistant_message)
                _publish_messages(conversation_id, user_id, [_serialize_message(assistant_message)])
                assistant_text = assistant_message.content_md or assistant_text
                assistant_message_id = assistant_message.id
            else:
                assistant_text = ai_message.content_md or assistant_text
                assistant_message_id = ai_message.id

            conversation.pending_interrupt_id = None
            db.add(conversation)
            db.commit()
            update_conversation_memory(
                db,
                conversation_id=conversation_id,
                user_content=content,
                assistant_content=assistant_text,
                message_id=assistant_message_id,
                updated_from="turn",
                actor_user_id=user_id,
            )
            retain_started = perf_counter()
            hindsight_service.retain_turn(
                user_id=user_id,
                conversation_id=conversation_id,
                user_content=content,
                assistant_content=assistant_text,
                tool_outputs=result.get("tool_outputs"),
            )
            logger.info(
                "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=memory_retain_done elapsed_ms=%s updated_from=turn",
                conversation_id,
                user_id,
                requested_backend,
                work_kind,
                int((perf_counter() - retain_started) * 1000),
            )
            _record_backend_run(
                db,
                conversation_id=conversation_id,
                user_id=user_id,
                work_kind=work_kind,
                requested_backend=requested_backend,
                actual_backend=requested_backend,
                fallback_backend=None,
                status="success",
                latency_ms=elapsed_ms,
            )
            return {"accepted": True, "requires_interrupt_decision": False}
        except Exception as exc:  # noqa: BLE001
            err = _format_exception(exc)
            logger.exception(
                "Primary backend failed conversation=%s user=%s backend=%s work_kind=%s",
                conversation_id,
                user_id,
                requested_backend,
                work_kind,
            )
            fallback_backend_name = resolve_fallback_backend_name(requested_backend)
            if fallback_backend_name:
                fallback_backend = get_backend(fallback_backend_name)
                try:
                    _ensure_conversation_daemon(conversation)
                    retry_started = perf_counter()
                    retry_content = _compose_effective_content(
                        content=content,
                        memory_context=memory_context,
                    )
                    logger.info(
                        "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=fallback_start fallback_backend=%s total_elapsed_ms=%s",
                        conversation_id,
                        user_id,
                        requested_backend,
                        work_kind,
                        fallback_backend_name,
                        int((perf_counter() - request_started) * 1000),
                    )
                    retry_result = fallback_backend.run_turn(
                        conversation_id=conversation_id,
                        content=retry_content,
                        on_progress=instrumented_on_progress,
                    )
                    retry_elapsed_ms = int((perf_counter() - retry_started) * 1000)
                    logger.info(
                        "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=fallback_done fallback_backend=%s backend_elapsed_ms=%s total_elapsed_ms=%s",
                        conversation_id,
                        user_id,
                        requested_backend,
                        work_kind,
                        fallback_backend_name,
                        retry_elapsed_ms,
                        int((perf_counter() - request_started) * 1000),
                    )
                    fallback_answer = str(retry_result.get("answer") or "")
                    fallback_message_id: str | None = None
                    ai_message = flush_ai(
                        force=True,
                        final_status="done",
                        tokens={
                            "input_tokens": int(retry_result.get("input_tokens", 0) or 0),
                            "output_tokens": int(retry_result.get("output_tokens", 0) or 0),
                            "total_tokens": int(retry_result.get("total_tokens", 0) or 0),
                        },
                        run_duration_ms=retry_elapsed_ms,
                    )
                    if not ai_message:
                        assistant_message = ConversationMessage(
                            conversation_id=conversation_id,
                            sender_user_id=None,
                            sender_role="assistant",
                            message_type="AIMessage",
                            message_status="done",
                            content_md=fallback_answer,
                            input_tokens=int(retry_result.get("input_tokens", 0) or 0),
                            output_tokens=int(retry_result.get("output_tokens", 0) or 0),
                            total_tokens=int(retry_result.get("total_tokens", 0) or 0),
                            run_duration_ms=retry_elapsed_ms,
                        )
                        db.add(assistant_message)
                        db.commit()
                        db.refresh(assistant_message)
                        _publish_messages(conversation_id, user_id, [_serialize_message(assistant_message)])
                        fallback_answer = assistant_message.content_md or fallback_answer
                        fallback_message_id = assistant_message.id
                    else:
                        fallback_answer = ai_message.content_md or fallback_answer
                        fallback_message_id = ai_message.id

                    conversation.pending_interrupt_id = None
                    db.add(conversation)
                    db.commit()
                    update_conversation_memory(
                        db,
                        conversation_id=conversation_id,
                        user_content=content,
                        assistant_content=fallback_answer,
                        message_id=fallback_message_id,
                        updated_from="fallback",
                        actor_user_id=user_id,
                    )
                    retain_started = perf_counter()
                    hindsight_service.retain_turn(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        user_content=content,
                        assistant_content=fallback_answer,
                        tool_outputs=retry_result.get("tool_outputs"),
                    )
                    logger.info(
                        "chat_timing conversation=%s user=%s backend=%s work_kind=%s stage=memory_retain_done elapsed_ms=%s updated_from=fallback",
                        conversation_id,
                        user_id,
                        fallback_backend_name,
                        work_kind,
                        int((perf_counter() - retain_started) * 1000),
                    )
                    _record_backend_run(
                        db,
                        conversation_id=conversation_id,
                        user_id=user_id,
                        work_kind=work_kind,
                        requested_backend=requested_backend,
                        actual_backend=fallback_backend_name,
                        fallback_backend=fallback_backend_name,
                        status="fallback_success",
                        latency_ms=retry_elapsed_ms,
                        error=err,
                    )
                    return {
                        "accepted": True,
                        "requires_interrupt_decision": False,
                        "fallback_backend": fallback_backend_name,
                    }
                except Exception as fallback_exc:  # noqa: BLE001
                    logger.exception(
                        "Fallback backend failed conversation=%s user=%s requested=%s fallback=%s work_kind=%s",
                        conversation_id,
                        user_id,
                        requested_backend,
                        fallback_backend_name,
                        work_kind,
                    )
                    err = f"{err}; fallback failed: {_format_exception(fallback_exc)}"
            error_message = ConversationMessage(
                conversation_id=conversation_id,
                sender_user_id=None,
                sender_role="assistant",
                message_type="SystemMessage",
                message_status="failed",
                content_md=t("system.deepagents_failed", error=err),
                run_duration_ms=int((perf_counter() - started) * 1000),
            )
            db.add(error_message)
            db.commit()
            db.refresh(error_message)
            _publish_messages(conversation_id, user_id, [_serialize_message(error_message)])
            update_conversation_memory(
                db,
                conversation_id=conversation_id,
                user_content=content,
                assistant_content=error_message.content_md,
                message_id=error_message.id,
                updated_from="error",
                actor_user_id=user_id,
            )
            _record_backend_run(
                db,
                conversation_id=conversation_id,
                user_id=user_id,
                work_kind=work_kind,
                requested_backend=requested_backend,
                actual_backend=requested_backend,
                fallback_backend=fallback_backend_name,
                status="failed",
                latency_ms=int((perf_counter() - started) * 1000),
                error=err,
            )
            return {"accepted": False, "error": err, "message": _serialize_message(error_message)}


def execute_interrupt_resume_and_persist(
    conversation_id: str,
    interrupt_id: str,
    decision: str,
    user_id: str,
) -> dict:
    from backend.api.routes.conversations import _resume_interrupt_and_persist

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        if not conversation or conversation.user_id != user_id:
            return {"accepted": False, "error": "Conversation not found"}
        requested_backend, backend = resolve_backend_for_conversation(conversation)
        _ensure_conversation_daemon(conversation)
        if requested_backend != "deepagents":
            return backend.resume_interrupt(
                conversation_id=conversation_id,
                interrupt_id=interrupt_id,
                decision=decision,
                on_progress=None,
            )
        return _resume_interrupt_and_persist(conversation, interrupt_id, decision, user_id, db)


def compute_next_run(task: ScheduledTask):
    if task.schedule_type == "once":
        return None
    if task.schedule_type == "interval":
        return datetime.now(timezone.utc) + timedelta(seconds=max(1, int(task.schedule_value)))
    if task.schedule_type == "cron":
        return _compute_next_cron_run(task.schedule_value)
    return None


def execute_scheduled_task_and_persist(task_id: str) -> dict:
    with SessionLocal() as db:
        task = db.get(ScheduledTask, task_id)
        if not task or task.status != "active":
            return {"accepted": False, "error": "Task not found"}
        conversation = db.get(Conversation, task.conversation_id)
        if not conversation or conversation.user_id != task.owner_user_id:
            return {"accepted": False, "error": "Conversation not found"}

        started = perf_counter()
        if _is_reminder_task(task):
            result = _deliver_scheduled_reminder(db, task=task, conversation=conversation)
        else:
            result = execute_user_message_and_persist(
                task.conversation_id,
                task.owner_user_id,
                task.prompt,
                work_kind="scheduled_task",
            )
        duration_ms = int((perf_counter() - started) * 1000)
        task.last_run = datetime.now(timezone.utc)
        task.next_run = compute_next_run(task)
        task.last_result = str(result)
        if task.next_run is None:
            task.status = "completed"
        db.add(task)
        db.add(
            ScheduledTaskRunLog(
                task_id=task.id,
                duration_ms=duration_ms,
                status="success" if result.get("accepted") else "failed",
                result=str(result)[:4000],
                error=None if result.get("accepted") else str(result.get("error") or "unknown"),
            )
        )
        db.commit()
        return result
