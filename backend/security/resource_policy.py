from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    reason: str | None = None


def allow() -> PolicyDecision:
    return PolicyDecision(True, None)


def deny(reason: str) -> PolicyDecision:
    return PolicyDecision(False, reason)


def require_allowed(
    decision: PolicyDecision,
    *,
    status_code: int = status.HTTP_403_FORBIDDEN,
) -> None:
    if decision.allowed:
        return
    raise HTTPException(status_code=status_code, detail=decision.reason or "Not allowed")
