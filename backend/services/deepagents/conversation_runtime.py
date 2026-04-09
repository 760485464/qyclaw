from __future__ import annotations

from contextvars import ContextVar
import json
import logging
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from langgraph.types import Command

from backend.i18n import t
_active_conversation_id: ContextVar[str | None] = ContextVar("_active_conversation_id", default=None)
logger = logging.getLogger(__name__)


class ConversationRuntimeMixin:
    @staticmethod
    def _iter_stream_events(chunk: Any) -> list[tuple[str | None, Any]]:
        if isinstance(chunk, tuple):
            if len(chunk) == 2 and isinstance(chunk[0], str):
                return [(chunk[0], chunk[1])]
            if len(chunk) == 3 and isinstance(chunk[1], str):
                return [(chunk[1], chunk[2])]
        return [(None, chunk)]

    @staticmethod
    def _extract_token_text(token: Any) -> str:
        if isinstance(token, str):
            return token
        content = getattr(token, "content", None)
        if content is not None:
            return ConversationRuntimeMixin._extract_text(content)
        return ConversationRuntimeMixin._extract_text(token)

    @staticmethod
    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, dict) and "text" in item:
                    chunks.append(str(item["text"]))
            return "".join(chunks)
        return str(content or "")

    @staticmethod
    def _iter_chunk_messages(chunk: dict[str, Any]) -> list[Any]:
        messages: list[Any] = []
        top_messages = chunk.get("messages")
        if isinstance(top_messages, (list, tuple)):
            messages.extend(top_messages)

        for value in chunk.values():
            if isinstance(value, dict):
                nested = value.get("messages")
                if isinstance(nested, (list, tuple)):
                    messages.extend(nested)
        return messages

    def _run_stream(
        self,
        conversation_id: str,
        stream_input: Any,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        started = perf_counter()
        agent = self._get_agent(conversation_id)

        config = {"configurable": {"thread_id": conversation_id}}
        accumulated_text: list[str] = []
        tool_outputs: list[dict[str, str]] = []
        seen_tool_outputs: set[str] = set()
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        first_stream_event_ms: int | None = None
        first_tool_output_ms: int | None = None
        stream_event_count = 0

        token = _active_conversation_id.set(conversation_id)
        try:
            stream_iter = agent.stream(
                stream_input,
                config=config,
                stream_mode=["messages", "updates"],
            )
            for chunk in stream_iter:
                if first_stream_event_ms is None:
                    first_stream_event_ms = int((perf_counter() - started) * 1000)
                    logger.info(
                        "chat_timing conversation=%s stage=deepagents_first_stream_event elapsed_ms=%s",
                        conversation_id,
                        first_stream_event_ms,
                    )
                for mode, payload in self._iter_stream_events(chunk):
                    stream_event_count += 1
                    if mode == "messages":
                        if not isinstance(payload, tuple) or len(payload) != 2:
                            continue
                        msg, metadata = payload
                        content = self._extract_token_text(msg)
                        if content:
                            accumulated_text.append(content)
                            if on_progress:
                                try:
                                    on_progress({"type": "ai_chunk", "content": content})
                                except Exception:
                                    pass

                        usage = None
                        if hasattr(msg, "response_metadata") and isinstance(msg.response_metadata, dict):
                            usage = msg.response_metadata.get("token_usage") or msg.response_metadata.get("usage")
                        if not usage and hasattr(msg, "usage_metadata") and isinstance(msg.usage_metadata, dict):
                            usage = msg.usage_metadata
                        if not usage and isinstance(metadata, dict):
                            usage = metadata.get("token_usage") or metadata.get("usage")

                        if usage:
                            input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", input_tokens)) or 0)
                            output_tokens = int(
                                usage.get("completion_tokens", usage.get("output_tokens", output_tokens)) or 0
                            )
                            total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
                        continue

                    if not isinstance(payload, dict):
                        continue

                    messages = self._iter_chunk_messages(payload)
                    for msg in messages:
                        content = self._extract_text(getattr(msg, "content", ""))
                        msg_type = getattr(msg, "type", "")

                        if msg_type in {"tool", "ToolMessage"} and content:
                            tool_name = (
                                getattr(msg, "name", None)
                                or getattr(msg, "tool_name", None)
                                or (getattr(msg, "additional_kwargs", {}) or {}).get("name")
                                or "unknown_tool"
                            )
                            dedupe_key = f"{tool_name}\n{content}"
                            if dedupe_key not in seen_tool_outputs:
                                seen_tool_outputs.add(dedupe_key)
                                tool_outputs.append(
                                    {
                                        "tool_name": str(tool_name),
                                        "content": content,
                                    }
                                )
                                if first_tool_output_ms is None:
                                    first_tool_output_ms = int((perf_counter() - started) * 1000)
                                    logger.info(
                                        "chat_timing conversation=%s stage=deepagents_first_tool_output elapsed_ms=%s tool_name=%s",
                                        conversation_id,
                                        first_tool_output_ms,
                                        str(tool_name),
                                    )
                                if on_progress:
                                    try:
                                        on_progress(
                                            {
                                                "type": "tool_output",
                                                "tool_name": str(tool_name),
                                                "content": content,
                                            }
                                        )
                                    except Exception:
                                        pass

                        usage = None
                        if hasattr(msg, "response_metadata") and isinstance(msg.response_metadata, dict):
                            usage = msg.response_metadata.get("token_usage") or msg.response_metadata.get("usage")
                        if not usage and hasattr(msg, "usage_metadata") and isinstance(msg.usage_metadata, dict):
                            usage = msg.usage_metadata

                        if usage:
                            input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", input_tokens)) or 0)
                            output_tokens = int(
                                usage.get("completion_tokens", usage.get("output_tokens", output_tokens)) or 0
                            )
                            total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)

                    if "__interrupt__" in payload:
                        interrupts = payload["__interrupt__"][0].value
                        if conversation_id in self._allow_all_conversations:
                            action_requests = interrupts.get("action_requests", [])
                            decisions = [{"type": "approve"} for _ in action_requests]
                            logger.info(
                                "chat_timing conversation=%s stage=deepagents_auto_resume_interrupt elapsed_ms=%s action_count=%s",
                                conversation_id,
                                int((perf_counter() - started) * 1000),
                                len(action_requests),
                            )
                            return self._run_stream(conversation_id, Command(resume={"decisions": decisions}), on_progress)

                        interrupt_id = str(uuid4())
                        self._pending_interrupts[interrupt_id] = {
                            "conversation_id": conversation_id,
                            "interrupts": interrupts,
                        }
                        self._conversation_pending_interrupt[conversation_id] = interrupt_id
                        logger.info(
                            "chat_timing conversation=%s stage=deepagents_interrupt elapsed_ms=%s action_count=%s",
                            conversation_id,
                            int((perf_counter() - started) * 1000),
                            len(interrupts.get("action_requests", [])),
                        )
                        return {
                            "interrupted": True,
                            "interrupt_id": interrupt_id,
                            "interrupts": interrupts,
                            "partial_answer": "".join(accumulated_text).strip(),
                            "tool_outputs": tool_outputs,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": total_tokens,
                        }
        finally:
            _active_conversation_id.reset(token)

        answer = "".join(accumulated_text).strip()
        if not answer:
            # Fallback to latest state messages when stream chunks don't carry the final AI text.
            try:
                state = agent.get_state(config)
                state_messages = (state.values or {}).get("messages", []) if state else []
                for msg in reversed(state_messages):
                    msg_type = getattr(msg, "type", "")
                    if msg_type in {"ai", "AIMessage"}:
                        content = self._extract_text(getattr(msg, "content", ""))
                        if content:
                            answer = content
                            break
            except Exception:
                pass

        if not answer:
            if tool_outputs:
                last = tool_outputs[-1]
                answer = t(
                    "tool.completed",
                    tool_name=last.get("tool_name", "unknown_tool"),
                    content=last.get("content", ""),
                )
            else:
                answer = t("tool.no_ai_output")

        logger.info(
            "chat_timing conversation=%s stage=deepagents_stream_done elapsed_ms=%s first_stream_event_ms=%s first_tool_output_ms=%s tool_output_count=%s stream_event_count=%s answer_chars=%s",
            conversation_id,
            int((perf_counter() - started) * 1000),
            first_stream_event_ms,
            first_tool_output_ms,
            len(tool_outputs),
            stream_event_count,
            len(answer),
        )
        return {
            "interrupted": False,
            "answer": answer,
            "tool_outputs": tool_outputs,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "model": self._model_label,
        }

    def run_turn(
        self,
        conversation_id: str,
        content: str,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return self._run_stream(
            conversation_id=conversation_id,
            stream_input={"messages": [{"role": "user", "content": content}]},
            on_progress=on_progress,
        )

    def resume_interrupt(
        self,
        conversation_id: str,
        interrupt_id: str,
        decision: str,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        pending = self._pending_interrupts.get(interrupt_id)
        if not pending:
            # Fallback: if frontend carries stale interrupt_id, use the latest pending one for this conversation.
            latest_id = self._conversation_pending_interrupt.get(conversation_id)
            if latest_id:
                pending = self._pending_interrupts.get(latest_id)
                if pending:
                    interrupt_id = latest_id

        if not pending or pending.get("conversation_id") != conversation_id:
            if interrupt_id in self._resolved_interrupts:
                return {
                    "interrupted": False,
                    "answer": t("interrupt.already_handled"),
                    "tool_outputs": [],
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "model": self._model_label,
                }
            raise ValueError(
                t("interrupt.not_found")
            )

        interrupts = pending["interrupts"]
        action_requests = interrupts.get("action_requests", [])

        if decision == "reject":
            del self._pending_interrupts[interrupt_id]
            self._conversation_pending_interrupt.pop(conversation_id, None)
            self._resolved_interrupts.add(interrupt_id)
            return {
                "interrupted": False,
                "rejected": True,
                "answer": t("interrupt.rejected"),
                "tool_outputs": [],
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "model": self._model_label,
            }

        if decision == "allow_all":
            self._allow_all_conversations.add(conversation_id)

        decisions = [{"type": "approve"} for _ in action_requests]
        del self._pending_interrupts[interrupt_id]
        self._conversation_pending_interrupt.pop(conversation_id, None)
        self._resolved_interrupts.add(interrupt_id)

        return self._run_stream(
            conversation_id=conversation_id,
            stream_input=Command(resume={"decisions": decisions}),
            on_progress=on_progress,
        )

    def format_interrupt_message(self, interrupt_payload: dict[str, Any]) -> str:
        action_requests = interrupt_payload.get("action_requests", [])
        lines = [t("interrupt.prompt_header")]
        for i, action in enumerate(action_requests, start=1):
            name = action.get("name", "unknown")
            args = action.get("args", {})
            cmd = args.get("commands") or args.get("command")
            if isinstance(cmd, list):
                cmd = "; ".join(str(c) for c in cmd)
            if cmd:
                lines.append(f"{i}. {name}: {cmd}")
            else:
                args_text = json.dumps(args, ensure_ascii=False)
                lines.append(f"{i}. {name} args={args_text}")

        return "\n".join(lines)

    def get_pending_interrupt_id(self, conversation_id: str) -> str | None:
        return self._conversation_pending_interrupt.get(conversation_id)
