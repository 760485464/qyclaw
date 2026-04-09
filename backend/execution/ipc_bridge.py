from __future__ import annotations

from typing import Any


class ContainerIpcBridge:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def execute_command(self, conversation_id: str, command: str) -> str:
        return self._runtime.execute(conversation_id, command)

    def status(self, conversation_id: str) -> dict[str, Any]:
        return self._runtime.status(conversation_id)
