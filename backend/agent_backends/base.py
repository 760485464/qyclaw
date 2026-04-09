from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from sqlalchemy.orm import Session


class AgentBackend(ABC):
    @abstractmethod
    def ensure_ready(self, conversation_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def run_turn(
        self,
        conversation_id: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def resume_interrupt(
        self,
        conversation_id: str,
        interrupt_id: str,
        decision: str,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def debug_exec(self, conversation_id: str, command: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def shutdown(self, conversation_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_conversation_daemon(
        self,
        conversation_id: str,
        daemon_cfg: dict[str, Any] | None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def prepare_conversation_skills(
        self,
        conversation_id: str,
        user_id: str,
        db: Session,
    ) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def format_interrupt_message(self, interrupt_payload: dict[str, Any]) -> str:
        raise NotImplementedError
