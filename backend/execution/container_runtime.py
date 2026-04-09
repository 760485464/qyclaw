from __future__ import annotations

from typing import Any, Callable

from backend.services.deepagents.docker_manager import DockerExecutionManager


class ConversationContainerRuntime:
    def __init__(
        self,
        config: dict[str, Any],
        daemon_resolver: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self._manager = DockerExecutionManager(config, daemon_resolver)

    def set_conversation_volumes(self, conversation_id: str, volumes: list[str]) -> None:
        self._manager.set_conversation_volumes(conversation_id, volumes)

    def clear_conversation_volumes(self, conversation_id: str) -> None:
        self._manager.clear_conversation_volumes(conversation_id)

    def execute(self, conversation_id: str, command: str) -> str:
        return self._manager.execute(conversation_id, command)

    def status(self, conversation_id: str) -> dict[str, Any]:
        return self._manager.status(conversation_id)

    def cleanup_conversation(self, conversation_id: str) -> None:
        self._manager.cleanup_conversation(conversation_id)

    def cleanup_all(self) -> None:
        self._manager.cleanup_all()
