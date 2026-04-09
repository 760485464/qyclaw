from __future__ import annotations

import logging
from threading import Lock
from threading import Thread
from time import perf_counter
from typing import Any

from backend.core.config import get_settings

logger = logging.getLogger(__name__)

try:
    from hindsight_client import Hindsight
except ImportError:  # pragma: no cover - optional dependency during rollout
    Hindsight = None  # type: ignore[assignment]


class HindsightService:
    def __init__(self) -> None:
        self._ensured_banks: set[str] = set()
        self._lock = Lock()
        self._retain_counts: dict[str, int] = {}
        self._reflect_in_progress: set[str] = set()

    def is_enabled(self) -> bool:
        settings = get_settings()
        cfg = settings.hindsight
        return bool(cfg.enabled and cfg.base_url and Hindsight is not None)

    def get_user_bank_id(self, user_id: str) -> str:
        cfg = get_settings().hindsight
        return f"{cfg.user_bank_prefix}:{user_id}"

    def _create_client(self) -> Hindsight | None:
        if not self.is_enabled():
            return None
        cfg = get_settings().hindsight
        return Hindsight(
            base_url=cfg.base_url.rstrip("/"),
            api_key=cfg.api_key,
            timeout=float(cfg.timeout),
        )

    def _close_client(self, client: Hindsight | None) -> None:
        if client is None:
            return
        try:
            client.close()
        except Exception:  # noqa: BLE001
            logger.exception("hindsight.close_client_failed")

    def _ensure_bank(self, client: Hindsight, bank_id: str, *, mission: str) -> bool:
        with self._lock:
            if bank_id in self._ensured_banks:
                return True
        try:
            client.create_bank(bank_id=bank_id, mission=mission)
            with self._lock:
                self._ensured_banks.add(bank_id)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("hindsight.ensure_bank_failed bank_id=%s", bank_id)
            return False

    @staticmethod
    def _normalize_text(value: str | None, *, limit: int | None = None) -> str:
        text = " ".join(str(value or "").split()).strip()
        if limit and len(text) > limit:
            return text[: limit - 3].rstrip() + "..."
        return text

    @staticmethod
    def _render_recall_block(title: str, response: Any) -> str:
        results = getattr(response, "results", None) or []
        lines: list[str] = []
        for item in results[:6]:
            text = " ".join(str(getattr(item, "text", "") or "").split()).strip()
            if text:
                lines.append(f"- {text}")
        if not lines:
            return ""
        return f"[{title}]\n" + "\n".join(lines)

    def _record_retain_and_maybe_schedule_reflect(self, *, user_id: str, conversation_id: str) -> None:
        cfg = get_settings().hindsight
        threshold = max(0, int(cfg.reflect_every_turns or 0))
        if threshold <= 0:
            return
        should_schedule = False
        count = 0
        with self._lock:
            count = int(self._retain_counts.get(user_id, 0)) + 1
            self._retain_counts[user_id] = count
            if count % threshold == 0 and user_id not in self._reflect_in_progress:
                self._reflect_in_progress.add(user_id)
                should_schedule = True
        if not should_schedule:
            return
        logger.info(
            "chat_timing conversation=%s user=%s stage=hindsight_reflect_scheduled retain_count=%s threshold=%s",
            conversation_id,
            user_id,
            count,
            threshold,
        )
        Thread(
            target=self._run_reflect_for_user,
            kwargs={"user_id": user_id, "conversation_id": conversation_id, "retain_count": count},
            name=f"hindsight-reflect-{user_id[:8]}",
            daemon=True,
        ).start()

    def _run_reflect_for_user(self, *, user_id: str, conversation_id: str, retain_count: int) -> None:
        started = perf_counter()
        client = self._create_client()
        if client is None:
            with self._lock:
                self._reflect_in_progress.discard(user_id)
            return
        try:
            bank_id = self.get_user_bank_id(user_id)
            if not self._ensure_bank(
                client,
                bank_id,
                mission="Persistent long-term memory for a single Qyclaw user.",
            ):
                return
            response = client.reflect(
                bank_id=bank_id,
                query=(
                    "Summarize the user's stable identity, preferences, recurring goals, work style, "
                    "and durable context that would help future assistant turns. Ignore ephemeral details."
                ),
                budget="low",
                max_tokens=600,
                include_facts=False,
            )
            answer = self._normalize_text(getattr(response, "answer", "") or "", limit=1200)
            logger.info(
                "chat_timing conversation=%s user=%s stage=hindsight_reflect_done elapsed_ms=%s retain_count=%s answer_chars=%s",
                conversation_id,
                user_id,
                int((perf_counter() - started) * 1000),
                retain_count,
                len(answer),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "hindsight.reflect_user_failed conversation=%s user=%s retain_count=%s",
                conversation_id,
                user_id,
                retain_count,
            )
        finally:
            self._close_client(client)
            with self._lock:
                self._reflect_in_progress.discard(user_id)

    def recall_for_turn(self, *, user_id: str, conversation_id: str, query: str) -> str:
        started = perf_counter()
        client = self._create_client()
        if client is None:
            return ""
        try:
            cfg = get_settings().hindsight
            sections: list[str] = []
            user_bank_id = self.get_user_bank_id(user_id)
            user_query = self._normalize_text(query, limit=600)

            if user_query and self._ensure_bank(
                client,
                user_bank_id,
                mission="Persistent long-term memory for a single Qyclaw user.",
            ):
                try:
                    user_started = perf_counter()
                    user_response = client.recall(
                        bank_id=user_bank_id,
                        query=user_query,
                        budget=cfg.recall_budget,
                        max_tokens=cfg.recall_max_tokens,
                    )
                    logger.info(
                        "chat_timing conversation=%s user=%s stage=hindsight_recall_user elapsed_ms=%s",
                        conversation_id,
                        user_id,
                        int((perf_counter() - user_started) * 1000),
                    )
                    block = self._render_recall_block("Hindsight User Memory", user_response)
                    if block:
                        sections.append(block)
                except Exception:  # noqa: BLE001
                    logger.exception("hindsight.recall_user_failed bank_id=%s", user_bank_id)

            if not sections:
                logger.info(
                    "chat_timing conversation=%s user=%s stage=hindsight_recall_done elapsed_ms=%s blocks=0",
                    conversation_id,
                    user_id,
                    int((perf_counter() - started) * 1000),
                )
                return ""
            logger.info(
                "chat_timing conversation=%s user=%s stage=hindsight_recall_done elapsed_ms=%s blocks=%s",
                conversation_id,
                user_id,
                int((perf_counter() - started) * 1000),
                len(sections),
            )
            return (
                "Use the following Hindsight memory as supporting context. "
                "Prefer it for persistent user facts and relevant prior work, unless the current message corrects it.\n\n"
                + "\n\n".join(sections)
            )
        finally:
            self._close_client(client)

    def retain_turn(
        self,
        *,
        user_id: str,
        conversation_id: str,
        user_content: str,
        assistant_content: str,
        tool_outputs: list[dict[str, str]] | None = None,
    ) -> None:
        started = perf_counter()
        client = self._create_client()
        if client is None:
            return
        try:
            cfg = get_settings().hindsight
            user_bank_id = self.get_user_bank_id(user_id)
            normalized_user = self._normalize_text(user_content)
            normalized_assistant = self._normalize_text(assistant_content)
            normalized_tools = tool_outputs or []

            if not normalized_user and not normalized_assistant and not normalized_tools:
                return

            common_tags = [f"user:{user_id}", f"conversation:{conversation_id}"]
            items: list[dict[str, Any]] = []
            if normalized_user:
                items.append(
                    {
                        "content": normalized_user,
                        "context": "user_message",
                        "metadata": {
                            "user_id": user_id,
                            "conversation_id": conversation_id,
                            "role": "user",
                        },
                        "tags": [*common_tags, "role:user"],
                    }
                )
            if normalized_assistant:
                items.append(
                    {
                        "content": normalized_assistant,
                        "context": "assistant_answer",
                        "metadata": {
                            "user_id": user_id,
                            "conversation_id": conversation_id,
                            "role": "assistant",
                        },
                        "tags": [*common_tags, "role:assistant"],
                    }
                )
            for output in normalized_tools[:8]:
                tool_name = self._normalize_text(output.get("tool_name"), limit=80) or "unknown_tool"
                tool_content = self._normalize_text(output.get("content"), limit=2000)
                if not tool_content:
                    continue
                items.append(
                    {
                        "content": f"{tool_name}: {tool_content}",
                        "context": "tool_output",
                        "metadata": {
                            "user_id": user_id,
                            "conversation_id": conversation_id,
                            "role": "tool",
                            "tool_name": tool_name,
                        },
                        "tags": [*common_tags, "role:tool", f"tool:{tool_name}"],
                    }
                )

            if not items:
                return

            user_items = [item for item in items if item["context"] != "tool_output"]
            if self._ensure_bank(
                client,
                user_bank_id,
                mission="Persistent long-term memory for a single Qyclaw user.",
            ):
                try:
                    user_started = perf_counter()
                    client.retain_batch(
                        bank_id=user_bank_id,
                        items=user_items,
                        retain_async=cfg.retain_async,
                    )
                    logger.info(
                        "chat_timing conversation=%s user=%s stage=hindsight_retain_user elapsed_ms=%s items=%s retain_async=%s",
                        conversation_id,
                        user_id,
                        int((perf_counter() - user_started) * 1000),
                        len(user_items),
                        cfg.retain_async,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("hindsight.retain_user_failed bank_id=%s", user_bank_id)
            logger.info(
                "chat_timing conversation=%s user=%s stage=hindsight_retain_done elapsed_ms=%s",
                conversation_id,
                user_id,
                int((perf_counter() - started) * 1000),
            )
            self._record_retain_and_maybe_schedule_reflect(
                user_id=user_id,
                conversation_id=conversation_id,
            )
        finally:
            self._close_client(client)


hindsight_service = HindsightService()
