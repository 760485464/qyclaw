from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class ExtractedMemory:
    memory_type: str
    title: str | None
    content_md: str
    source: str = "conversation"
    source_kind: str = "auto_extract"


_PREFERENCE_PREFIXES = (
    "记住",
    "请记住",
    "帮我记住",
    "我喜欢",
    "我偏好",
    "remember",
    "please remember",
    "i prefer",
    "i like",
)

_TEMPORAL_MARKERS = (
    "今天",
    "刚刚",
    "这次",
    "临时",
    "today",
    "just now",
    "this time",
    "temporary",
)

_NAME_PATTERNS = (
    re.compile(r"^(?:我叫|我的名字是|你可以叫我|叫我)\s*[:：]?\s*(?P<name>[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_\-\s]{0,30})$"),
    re.compile(r"^(?:my name is|i am|i'm|call me)\s+(?P<name>[A-Za-z][A-Za-z0-9_\-\s]{0,30})$", re.IGNORECASE),
)


def _normalize_text(text: str, limit: int = 500) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) > limit:
        normalized = normalized[: limit - 3].rstrip() + "..."
    return normalized


def _extract_name_memory(text: str) -> list[ExtractedMemory]:
    for pattern in _NAME_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        name = " ".join(match.group("name").split()).strip(" ,.;，。；：:")
        if not name:
            return []
        if len(name) > 32:
            name = name[:32].rstrip()
        return [
            ExtractedMemory(
                memory_type="profile",
                title="User name",
                content_md=f"User's name is {name}.",
                source_kind="auto_extract",
            )
        ]
    return []


def _extract_preference_memory(text: str) -> list[ExtractedMemory]:
    lowered = text.lower()
    if not any(lowered.startswith(prefix.lower()) for prefix in _PREFERENCE_PREFIXES):
        return []
    if any(marker in lowered for marker in _TEMPORAL_MARKERS):
        return []
    if len(text.split()) > 40:
        return []
    return [
        ExtractedMemory(
            memory_type="preference",
            title="User preference",
            content_md=_normalize_text(text),
            source_kind="auto_extract",
        )
    ]


def extract_user_memory_candidates(content: str) -> list[ExtractedMemory]:
    text = _normalize_text(content)
    if not text:
        return []
    name_memories = _extract_name_memory(text)
    if name_memories:
        return name_memories
    return _extract_preference_memory(text)
