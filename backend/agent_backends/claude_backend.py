from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from backend.agent_backends.base import AgentBackend
from backend.core.config import get_settings


class ClaudeBackend(AgentBackend):
    _MODEL_ALIASES: dict[str, str] = {
        "claude-sonnet-4-5": "claude-sonnet-4-20250514",
        "claude-sonnet-4.5": "claude-sonnet-4-20250514",
        "sonnet-4-5": "claude-sonnet-4-20250514",
    }

    def _claude_config(self):
        return get_settings().claude_agent

    def _deepagents_backend(self):
        from backend.agent_backends.deepagents_backend import deepagents_backend

        return deepagents_backend

    def _sdk_components(self) -> tuple[Any, Any, Any]:
        try:
            import anyio
            from claude_agent_sdk import ClaudeAgentOptions, query
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Claude backend requires claude-agent-sdk. Install it with: pip install claude-agent-sdk"
            ) from exc
        return anyio, ClaudeAgentOptions, query

    def _resolve_model(self, conversation_id: str) -> str:
        _ = conversation_id
        configured = str(self._claude_config().model or "").strip()
        return self._normalize_model_name(configured or "claude-sonnet-4-20250514")

    def _normalize_model_name(self, model_name: str) -> str:
        raw = str(model_name or "").strip()
        if not raw:
            return "claude-sonnet-4-20250514"
        return self._MODEL_ALIASES.get(raw, raw)

    def _extract_message_text(self, message: Any) -> str:
        parts: list[str] = []
        for block in getattr(message, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        if parts:
            return "".join(parts)
        result = getattr(message, "result", None)
        return str(result or "")

    def ensure_ready(self, conversation_id: str) -> dict[str, Any]:
        cfg = self._claude_config()
        try:
            self._sdk_components()
            available = bool(cfg.enabled)
            error = None
        except Exception as exc:  # noqa: BLE001
            available = False
            error = str(exc)
        return {
            "enabled": available,
            "backend": "claude",
            "model": self._resolve_model(conversation_id),
            "cli_path": cfg.cli_path,
            "fallback_model": cfg.fallback_model,
            "permission_mode": cfg.permission_mode,
            "init_error": error,
        }

    def run_turn(
        self,
        conversation_id: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        anyio, ClaudeAgentOptions, query = self._sdk_components()
        cfg = self._claude_config()
        if not cfg.enabled:
            raise RuntimeError("Claude backend is disabled by config")
        model = self._resolve_model(conversation_id)

        async def _run() -> dict[str, Any]:
            chunks: list[str] = []
            final_result = ""
            env: dict[str, str] = {}
            if cfg.base_url:
                env["ANTHROPIC_BASE_URL"] = str(cfg.base_url)
            if cfg.api_key:
                env["ANTHROPIC_API_KEY"] = str(cfg.api_key)
            options = ClaudeAgentOptions(
                model=model,
                cli_path=cfg.cli_path,
                fallback_model=cfg.fallback_model,
                allowed_tools=[],
                permission_mode=cfg.permission_mode,
                max_turns=cfg.max_turns,
                max_thinking_tokens=cfg.max_thinking_tokens,
                effort=cfg.effort,
                env=env,
            )
            prompt_payload: str | Any = content
            image_attachments = [item for item in (attachments or []) if str(item.get("kind") or "") == "image"]
            if image_attachments:
                blocks: list[dict[str, Any]] = []
                if content.strip():
                    blocks.append({"type": "text", "text": content})
                for item in image_attachments:
                    local_path = Path(str(item.get("local_path") or "")).expanduser()
                    if not local_path.exists() or not local_path.is_file():
                        raise RuntimeError(f"Image attachment not found: {local_path}")
                    mime_type = str(item.get("content_type") or "image/png").strip() or "image/png"
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": base64.b64encode(local_path.read_bytes()).decode("ascii"),
                            },
                        }
                    )

                async def _prompt_stream():
                    yield {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": blocks,
                        },
                    }

                prompt_payload = _prompt_stream()

            async for message in query(prompt=prompt_payload, options=options):
                text = self._extract_message_text(message)
                if not text:
                    continue
                final_result = text
                if on_progress:
                    on_progress({"type": "ai_chunk", "content": text})
                chunks.append(text)
            answer = final_result or "".join(chunks).strip()
            return {
                "answer": answer,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "interrupted": False,
            }

        return anyio.run(_run)

    def resume_interrupt(
        self,
        conversation_id: str,
        interrupt_id: str,
        decision: str,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("Claude backend interrupt resume is not implemented yet")

    def debug_exec(self, conversation_id: str, command: str) -> dict[str, Any]:
        return self._deepagents_backend().debug_exec(conversation_id=conversation_id, command=command)

    def shutdown(self, conversation_id: str) -> None:
        self._deepagents_backend().shutdown(conversation_id)

    def set_conversation_daemon(
        self,
        conversation_id: str,
        daemon_cfg: dict[str, Any] | None,
    ) -> None:
        self._deepagents_backend().set_conversation_daemon(conversation_id, daemon_cfg)

    def prepare_conversation_skills(
        self,
        conversation_id: str,
        user_id: str,
        db: Session,
    ) -> list[str]:
        return self._deepagents_backend().prepare_conversation_skills(conversation_id, user_id, db=db)

    def format_interrupt_message(self, interrupt_payload: dict[str, Any]) -> str:
        return "Claude backend requested an interrupt."


claude_backend = ClaudeBackend()
