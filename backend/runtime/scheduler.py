from __future__ import annotations

from datetime import datetime, timezone
from logging import getLogger
from threading import Event, Thread
from time import sleep

from sqlalchemy import select

from backend.core.database import SessionLocal
from backend.core.models import ScheduledTask
from backend.runtime.queue_manager import queue_manager
from backend.runtime.work_items import WorkItem

logger = getLogger(__name__)


class RuntimeScheduler:
    def __init__(self) -> None:
        self._thread: Thread | None = None
        self._shutdown = Event()
        self._started = False
        self._interval_seconds = 5

    def start(self, interval_seconds: int = 5) -> None:
        if self._started:
            return
        self._interval_seconds = max(1, int(interval_seconds or 5))
        self._shutdown.clear()
        self._thread = Thread(target=self._run_loop, name="qyclaw-runtime-scheduler", daemon=True)
        self._thread.start()
        self._started = True
        logger.info("Runtime scheduler started (interval_seconds=%s)", self._interval_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        if not self._started:
            return
        self._shutdown.set()
        thread = self._thread
        if thread:
            thread.join(timeout=timeout)
        self._thread = None
        self._started = False
        logger.info("Runtime scheduler stopped")

    def _run_loop(self) -> None:
        while not self._shutdown.is_set():
            self._enqueue_due_tasks()
            sleep(self._interval_seconds)

    def _enqueue_due_tasks(self) -> None:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            tasks = db.scalars(
                select(ScheduledTask).where(
                    ScheduledTask.status == "active",
                    ScheduledTask.next_run.is_not(None),
                    ScheduledTask.next_run <= now,
                )
            ).all()
            for task in tasks:
                queue_manager.enqueue(
                    WorkItem(
                        kind="scheduled_task",
                        conversation_id=task.conversation_id,
                        user_id=task.owner_user_id,
                        payload={"task_id": task.id},
                    )
                )
                task.next_run = None
                db.add(task)
            db.commit()


scheduler = RuntimeScheduler()
