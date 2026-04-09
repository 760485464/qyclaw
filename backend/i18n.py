from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from backend.core.config import get_settings


@lru_cache(maxsize=8)
def _load_messages(locale: str) -> dict[str, Any]:
    base = Path(__file__).resolve().parent / "locales"
    path = base / f"{locale}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def get_locale() -> str:
    settings = get_settings()
    locale = getattr(settings.app, "locale", None) or "en"
    return str(locale).lower()


def _resolve_value(messages: dict[str, Any], key: str) -> Any:
    if key in messages:
        return messages[key]
    current: Any = messages
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def get_message(key: str, locale: str | None = None) -> Any:
    if locale is None:
        locale = get_locale()
    value = _resolve_value(_load_messages(str(locale)), key)
    if value is None and str(locale).lower() != "en":
        value = _resolve_value(_load_messages("en"), key)
    return value


def get_list(key: str, locales: tuple[str, ...] | None = None) -> list[str]:
    values: list[str] = []
    target_locales = locales or ("en", "zh")
    for locale in target_locales:
        value = _resolve_value(_load_messages(locale), key)
        if isinstance(value, list):
            values.extend([str(item) for item in value if item is not None])
        elif isinstance(value, str):
            values.append(value)
    seen: set[str] = set()
    deduped: list[str] = []
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def t(key: str, **kwargs: Any) -> str:
    value = get_message(key)
    if value is None:
        text = key
    else:
        text = str(value)
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text
