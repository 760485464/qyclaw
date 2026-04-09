from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

from backend.agent_backends.base import AgentBackend
from backend.core.database import SessionLocal
from backend.core.models import Conversation
from backend.services.deepagents_service import deepagent_service


class DeepAgentsBackend(AgentBackend):
    def _resolve_user_id(self, conversation_id: str) -> str | None:
        with SessionLocal() as db:
            conversation = db.get(Conversation, conversation_id)
            if not conversation or not conversation.user_id:
                return None
            return str(conversation.user_id)

    def ensure_ready(self, conversation_id: str) -> dict[str, Any]:
        with SessionLocal() as db:
            conversation = db.get(Conversation, conversation_id)
            if conversation and conversation.daemon_host:
                deepagent_service.set_conversation_daemon(
                    conversation_id,
                    {"host": conversation.daemon_host},
                )
        status = deepagent_service.ensure_conversation_ready(conversation_id)
        return status

    def run_turn(
        self,
        conversation_id: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if attachments:
            raise RuntimeError("DeepAgents backend does not support image multimodal messages yet")
        return deepagent_service.run_turn(
            conversation_id=conversation_id,
            content=content,
            on_progress=on_progress,
        )

    def resume_interrupt(
        self,
        conversation_id: str,
        interrupt_id: str,
        decision: str,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return deepagent_service.resume_interrupt(
            conversation_id=conversation_id,
            interrupt_id=interrupt_id,
            decision=decision,
            on_progress=on_progress,
        )

    def debug_exec(self, conversation_id: str, command: str) -> dict[str, Any]:
        return deepagent_service.debug_exec(
            conversation_id=conversation_id,
            command=command,
        )

    def shutdown(self, conversation_id: str) -> None:
        deepagent_service.cleanup_conversation(conversation_id)

    def set_conversation_daemon(
        self,
        conversation_id: str,
        daemon_cfg: dict[str, Any] | None,
    ) -> None:
        deepagent_service.set_conversation_daemon(conversation_id, daemon_cfg)

    def prepare_conversation_skills(
        self,
        conversation_id: str,
        user_id: str,
        db: Session,
    ) -> list[str]:
        return deepagent_service.prepare_conversation_skills(
            conversation_id,
            user_id,
            db=db,
        )

    def format_interrupt_message(self, interrupt_payload: dict[str, Any]) -> str:
        return deepagent_service.format_interrupt_message(interrupt_payload)


deepagents_backend = DeepAgentsBackend()
