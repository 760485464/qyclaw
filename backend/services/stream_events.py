from __future__ import annotations

from datetime import datetime, timezone
import queue
import threading
from typing import Any
from uuid import uuid4


class StreamEventPublisher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, dict[str, queue.Queue]] = {}
        self._shutdown = threading.Event()

    def publish_conversation_event(
        self,
        conversation_id: str,
        user_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        if self._shutdown.is_set():
            return False
        local_item = {
            "event_id": str(uuid4()),
            "conversation_id": conversation_id,
            "user_id": user_id,
            "event_type": event_type,
            "payload": payload,
            "ts": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        }
        self._publish_local(conversation_id, local_item)
        return True

    def _publish_local(self, conversation_id: str, item: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers.get(conversation_id, {}).values())
        for q in subscribers:
            try:
                q.put_nowait(item)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(item)
                except queue.Full:
                    pass

    def subscribe(self, conversation_id: str) -> tuple[str, queue.Queue]:
        if self._shutdown.is_set():
            raise RuntimeError("Stream publisher is shutting down")
        subscriber_id = str(uuid4())
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.setdefault(conversation_id, {})[subscriber_id] = q
        return subscriber_id, q

    def unsubscribe(self, conversation_id: str, subscriber_id: str) -> None:
        with self._lock:
            conv_map = self._subscribers.get(conversation_id)
            if not conv_map:
                return
            conv_map.pop(subscriber_id, None)
            if not conv_map:
                self._subscribers.pop(conversation_id, None)

    def is_shutdown(self) -> bool:
        return self._shutdown.is_set()

    def close_all(self) -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        shutdown_item = {
            "event_id": str(uuid4()),
            "event_type": "system.shutdown",
            "payload": {"message": "shutdown"},
            "ts": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        }
        with self._lock:
            subscribers = [
                q for conv_map in self._subscribers.values() for q in conv_map.values()
            ]
        for q in subscribers:
            try:
                q.put_nowait(shutdown_item)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(shutdown_item)
                except queue.Full:
                    pass


stream_event_publisher = StreamEventPublisher()
