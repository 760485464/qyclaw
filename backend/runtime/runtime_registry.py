from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock

from backend.runtime.state import ConversationRuntimeState


class RuntimeRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._states: dict[str, ConversationRuntimeState] = defaultdict(
            ConversationRuntimeState
        )

    def ensure(self, conversation_id: str) -> ConversationRuntimeState:
        with self._lock:
            return self._states[conversation_id]

    def on_enqueued(self, conversation_id: str) -> None:
        with self._lock:
            state = self._states[conversation_id]
            state.pending_count += 1

    def on_started(self, conversation_id: str, work_item_id: str) -> None:
        with self._lock:
            state = self._states[conversation_id]
            state.active = True
            state.current_work_item_id = work_item_id
            if state.pending_count > 0:
                state.pending_count -= 1

    def on_finished(self, conversation_id: str, retry_count: int) -> None:
        with self._lock:
            state = self._states[conversation_id]
            state.active = False
            state.current_work_item_id = None
            state.retry_count = retry_count
            state.last_active_at = datetime.now(timezone.utc)

    def is_active(self, conversation_id: str) -> bool:
        with self._lock:
            state = self._states.get(conversation_id)
            return bool(state and state.active)

    def snapshot(self, conversation_id: str) -> ConversationRuntimeState:
        with self._lock:
            state = self._states.get(conversation_id)
            if not state:
                return ConversationRuntimeState()
            return ConversationRuntimeState(
                active=state.active,
                current_work_item_id=state.current_work_item_id,
                retry_count=state.retry_count,
                pending_count=state.pending_count,
                last_active_at=state.last_active_at,
            )


runtime_registry = RuntimeRegistry()
