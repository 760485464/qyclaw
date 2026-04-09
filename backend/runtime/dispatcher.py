from __future__ import annotations

from typing import Callable

from backend.runtime.work_items import WorkItem

WorkItemHandler = Callable[[WorkItem], None]

_handlers: dict[str, WorkItemHandler] = {}


def register_handler(kind: str, handler: WorkItemHandler) -> None:
    _handlers[kind] = handler


def dispatch_item(item: WorkItem) -> None:
    handler = _handlers.get(item.kind)
    if handler is None:
        raise RuntimeError(f"No runtime handler registered for work item kind: {item.kind}")
    handler(item)
