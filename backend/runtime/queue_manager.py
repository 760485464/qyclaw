from __future__ import annotations

from collections import deque
from logging import getLogger
from threading import Condition, Event, Thread
from time import sleep

from backend.runtime.dispatcher import dispatch_item
from backend.runtime.runtime_registry import runtime_registry
from backend.runtime.work_items import WorkItem

logger = getLogger(__name__)


class QueueManager:
    def __init__(self) -> None:
        self._pending: deque[WorkItem] = deque()
        self._condition = Condition()
        self._shutdown = Event()
        self._started = False
        self._coordinator: Thread | None = None
        self._active_count = 0
        self._max_concurrency = 2
        self._max_retries = 3

    def start(self, max_concurrency: int = 2, max_retries: int = 3) -> None:
        with self._condition:
            if self._started:
                return
            self._max_concurrency = max(1, int(max_concurrency or 2))
            self._max_retries = max(0, int(max_retries or 3))
            self._shutdown.clear()
            self._coordinator = Thread(target=self._run_loop, name="qyclaw-runtime-queue", daemon=True)
            self._coordinator.start()
            self._started = True
            logger.info(
                "Runtime queue started (max_concurrency=%s, max_retries=%s)",
                self._max_concurrency,
                self._max_retries,
            )

    def stop(self, timeout: float = 5.0) -> None:
        with self._condition:
            if not self._started:
                return
            self._shutdown.set()
            self._condition.notify_all()
            coordinator = self._coordinator
        if coordinator:
            coordinator.join(timeout=timeout)
        with self._condition:
            self._started = False
            self._coordinator = None
            logger.info("Runtime queue stopped")

    def enqueue(self, item: WorkItem) -> None:
        runtime_registry.on_enqueued(item.conversation_id)
        with self._condition:
            self._pending.append(item)
            self._condition.notify_all()
        logger.info(
            "Enqueued work item kind=%s conversation=%s id=%s retry=%s",
            item.kind,
            item.conversation_id,
            item.id,
            item.retry_count,
        )

    def _run_loop(self) -> None:
        while not self._shutdown.is_set():
            item = self._next_dispatchable_item()
            if item is None:
                continue
            self._active_count += 1
            runtime_registry.on_started(item.conversation_id, item.id)
            worker = Thread(
                target=self._run_item,
                args=(item,),
                name=f"qyclaw-work-{item.conversation_id[:8]}",
                daemon=True,
            )
            worker.start()

    def _next_dispatchable_item(self) -> WorkItem | None:
        with self._condition:
            while not self._shutdown.is_set():
                if not self._pending or self._active_count >= self._max_concurrency:
                    self._condition.wait(timeout=1.0)
                    continue
                for item in list(self._pending):
                    if runtime_registry.is_active(item.conversation_id):
                        continue
                    self._pending.remove(item)
                    return item
                self._condition.wait(timeout=0.5)
        return None

    def _run_item(self, item: WorkItem) -> None:
        try:
            dispatch_item(item)
            runtime_registry.on_finished(item.conversation_id, item.retry_count)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Runtime work item failed kind=%s conversation=%s id=%s: %s",
                item.kind,
                item.conversation_id,
                item.id,
                exc,
            )
            retry_count = item.retry_count + 1
            runtime_registry.on_finished(item.conversation_id, retry_count)
            if retry_count <= self._max_retries and not self._shutdown.is_set():
                delay_seconds = min(30, 2 ** retry_count)
                sleep(delay_seconds)
                self.enqueue(
                    WorkItem(
                        kind=item.kind,
                        conversation_id=item.conversation_id,
                        user_id=item.user_id,
                        payload=dict(item.payload),
                        retry_count=retry_count,
                    )
                )
        finally:
            with self._condition:
                if self._active_count > 0:
                    self._active_count -= 1
                self._condition.notify_all()


queue_manager = QueueManager()
