from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ConversationRuntimeState:
    active: bool = False
    current_work_item_id: str | None = None
    retry_count: int = 0
    pending_count: int = 0
    last_active_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
