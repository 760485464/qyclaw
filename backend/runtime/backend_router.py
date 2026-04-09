from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

from backend.agent_backends import get_backend, normalize_backend_name
from backend.core.config import get_settings

if TYPE_CHECKING:
    from backend.core.models import Conversation


def _bucket_user(user_id: str) -> int:
    digest = sha256(str(user_id).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def resolve_backend_name(
    user_id: str,
    requested_backend: str | None = None,
) -> str:
    settings = get_settings()
    routing = settings.backend_routing

    if requested_backend:
        return normalize_backend_name(requested_backend)

    override = routing.user_overrides.get(user_id)
    if override:
        return normalize_backend_name(override)

    rollout_percent = max(0, min(int(routing.rollout_percent or 0), 100))
    rollout_backend = normalize_backend_name(routing.rollout_backend)
    if rollout_percent > 0 and _bucket_user(user_id) < rollout_percent:
        return rollout_backend

    return normalize_backend_name(routing.default_backend)


def resolve_backend_for_conversation(conversation: Conversation):
    backend_name = resolve_backend_name(
        user_id=conversation.user_id,
        requested_backend=getattr(conversation, "execution_backend", None),
    )
    return backend_name, get_backend(backend_name)


def resolve_fallback_backend_name(primary_backend: str) -> str | None:
    settings = get_settings()
    routing = settings.backend_routing
    if not routing.enable_fallback:
        return None
    fallback = normalize_backend_name(routing.fallback_backend)
    if fallback == normalize_backend_name(primary_backend):
        return None
    return fallback
