from __future__ import annotations

from backend.agent_backends.base import AgentBackend

_BACKEND_NAMES = {"deepagents", "claude"}


def normalize_backend_name(name: str | None) -> str:
    value = str(name or "deepagents").strip().lower()
    return value if value in _BACKEND_NAMES else "deepagents"


def get_backend(name: str | None) -> AgentBackend:
    normalized = normalize_backend_name(name)
    if normalized == "claude":
        from backend.agent_backends.claude_backend import claude_backend

        return claude_backend

    from backend.agent_backends.deepagents_backend import deepagents_backend

    return deepagents_backend


def available_backend_names() -> list[str]:
    return sorted(_BACKEND_NAMES)
