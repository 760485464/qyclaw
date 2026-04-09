from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

from deepagents.backends import FilesystemBackend
from sqlalchemy import or_, select

from backend.core.config import get_settings
from backend.core.database import SessionLocal
from backend.core.models import Conversation, ConversationSkillInstall, ConversationSkillSetting, Skill, User


class QyclawFilesystemBackend(FilesystemBackend):
    """Route workspace paths to per-conversation workspace while preserving repo-root virtual paths."""

    def __init__(
        self,
        repo_root: Path,
        workspace_dir: Path,
        workdir_alias: str = "/workspace",
        agent_skills_dir: Path | None = None,
        readonly_skills_dir: Path | None = None,
        builtin_skills_dir: Path | None = None,
        on_skill_access: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(root_dir=str(repo_root), virtual_mode=True)
        self.repo_root = repo_root.resolve()
        self.workspace_dir = workspace_dir.resolve()
        self.workdir_alias = "/" + workdir_alias.strip("/") if workdir_alias else "/workspace"
        self.agent_skills_dir = agent_skills_dir.resolve() if agent_skills_dir else None
        self.readonly_skills_dir = readonly_skills_dir.resolve() if readonly_skills_dir else None
        self.builtin_skills_dir = builtin_skills_dir.resolve() if builtin_skills_dir else None

    def _is_under(self, path: Path, base: Path | None) -> bool:
        if base is None:
            return False
        try:
            path.resolve().relative_to(base.resolve())
            return True
        except ValueError:
            return False

    def _assert_writable(self, path: Path) -> None:
        if self._is_under(path, self.readonly_skills_dir):
            raise ValueError("Skills directory is read-only")
        if self._is_under(path, self.builtin_skills_dir):
            raise ValueError("Builtin skills directory is read-only")

    def _workspace_relative(self, key: str) -> str | None:
        raw = str(key or "").strip()
        if raw in {"", ".", "./", "/"}:
            return ""

        alias_prefix = f"{self.workdir_alias}/"
        if raw == self.workdir_alias or raw == f"{self.workdir_alias}/":
            return ""
        if raw.startswith(alias_prefix):
            return raw[len(alias_prefix) :]

        if raw.startswith("workspace/"):
            return raw[len("workspace/") :]

        # Allow absolute paths (container-scoped). They will be resolved as-is.
        if raw.startswith("/"):
            return None

        # Route relative paths into conversation workspace by default.
        if not raw.startswith("/"):
            return raw
        return None

    def _resolve_path(self, key: str) -> Path:
        rel = self._workspace_relative(key)
        if rel is None:
            raw = str(key or "").strip()
            if not raw.startswith("/"):
                raise ValueError("Path is outside conversation workspace")
            resolved = Path(raw).resolve()
            return resolved

        if ".." in rel or rel.startswith("~"):
            raise ValueError("Path traversal not allowed")
        if self.agent_skills_dir and (rel == "agent_skills" or rel.startswith("agent_skills/")):
            suffix = rel[len("agent_skills/") :] if rel != "agent_skills" else ""
            resolved = (self.agent_skills_dir / suffix).resolve()
            try:
                resolved.relative_to(self.agent_skills_dir)
            except ValueError as exc:
                raise ValueError("Path traversal not allowed") from exc
            return resolved
        if self.builtin_skills_dir and (rel == "skills-builtin" or rel.startswith("skills-builtin/")):
            suffix = rel[len("skills-builtin/") :] if rel != "skills-builtin" else ""
            resolved = (self.builtin_skills_dir / suffix).resolve()
            try:
                resolved.relative_to(self.builtin_skills_dir)
            except ValueError as exc:
                raise ValueError("Path traversal not allowed") from exc
            return resolved

        resolved = (self.workspace_dir / rel).resolve()
        try:
            resolved.relative_to(self.workspace_dir)
        except ValueError as exc:
            raise ValueError("Path traversal not allowed") from exc
        return resolved

    def _to_virtual_path(self, path: Path) -> str:
        resolved = path.resolve()
        if self.agent_skills_dir and self._is_under(resolved, self.agent_skills_dir):
            rel = resolved.relative_to(self.agent_skills_dir).as_posix()
            return f"{self.workdir_alias}/agent_skills/{rel}".rstrip("/")
        if self.builtin_skills_dir and self._is_under(resolved, self.builtin_skills_dir):
            rel = resolved.relative_to(self.builtin_skills_dir).as_posix()
            return f"{self.workdir_alias}/skills-builtin/{rel}".rstrip("/")
        try:
            rel = resolved.relative_to(self.workspace_dir).as_posix()
            return self.workdir_alias if rel == "." else f"{self.workdir_alias}/{rel}"
        except ValueError:
            return super()._to_virtual_path(path)

    def write(self, file_path: str, content: str):  # type: ignore[override]
        resolved = self._resolve_path(file_path)
        self._assert_writable(resolved)
        return super().write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False):  # type: ignore[override]
        resolved = self._resolve_path(file_path)
        self._assert_writable(resolved)
        return super().edit(file_path, old_string, new_string, replace_all)

    def upload_files(self, files: list[tuple[str, bytes]]):  # type: ignore[override]
        for path, _ in files:
            resolved = self._resolve_path(path)
            self._assert_writable(resolved)
        return super().upload_files(files)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:  # type: ignore[override]
        return super().read(file_path, offset=offset, limit=limit)


class SkillsMixin:
    @staticmethod
    def _resolve_workspace_root(config_value: Any, default: Path) -> Path:
        path = Path(str(config_value or default)).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path

    def _conversation_workspace(self, conversation_id: str) -> Path:
        path = (self._workspace_root / conversation_id).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _conversation_skills_dir(self, conversation_id: str) -> Path:
        settings = get_settings()
        storage_dir = settings.skill_storage.conversationskills_dir
        base = Path(str(storage_dir)).expanduser()
        if not base.is_absolute():
            base = (Path.cwd() / base).resolve()
        base.mkdir(parents=True, exist_ok=True)
        path = (base / conversation_id).resolve()
        if not str(path).startswith(str(base)):
            raise ValueError("Invalid conversation skills path")
        return path

    def _resolve_skill_mount(
        self,
        conversation_id: str,
        skill_dir: Path,
        workspace_prefix: str,
    ) -> tuple[str, str]:
        resolved = skill_dir.resolve()
        try:
            rel = resolved.relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            rel = ""
        target_rel = rel or resolved.name
        target = f"{workspace_prefix}/{target_rel}".rstrip("/")
        daemon_host = self._resolve_daemon_host(conversation_id)
        if daemon_host:
            source = f"/{target_rel}"
        else:
            source = str(resolved)
        return source, target

    @staticmethod
    def _resolve_path(path_str: str) -> Path:
        return Path(path_str).expanduser().resolve()

    @staticmethod
    def _reset_dir_contents(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        for child in path.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    def _resolve_builtin_skills_dir(self, skills_cfg: dict[str, Any]) -> Path | None:
        source_dirs = skills_cfg.get("directories", [])
        resolved = [self._resolve_path(p) for p in source_dirs]
        valid = [p for p in resolved if p.exists() and p.is_dir()]
        if not valid:
            return None
        return valid[-1]

    @staticmethod
    def _format_with_line_numbers(lines: list[str], start_line: int = 1) -> str:
        width = len(str(start_line + len(lines) - 1))
        result = []
        for i, line in enumerate(lines, start=start_line):
            result.append(f"{str(i).rjust(width)} {line}")
        return "\n".join(result).rstrip()

    def _read_skill_doc(self, path: Path, offset: int = 0, limit: int = 2000) -> str:
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"Error: File '{path.as_posix()}' not found"
        except Exception as exc:  # noqa: BLE001
            return f"Error reading file '{path.as_posix()}': {exc}"

        lines = content.splitlines()
        if not lines:
            return "(Empty file)"
        try:
            start_idx = max(0, int(offset))
            max_lines = max(1, int(limit))
        except Exception:
            return "Error: offset/limit must be integers"
        end_idx = min(start_idx + max_lines, len(lines))
        if start_idx >= len(lines):
            return f"Error: Line offset {offset} exceeds file length ({len(lines)} lines)"
        return self._format_with_line_numbers(lines[start_idx:end_idx], start_line=start_idx + 1)

    def _resolve_skill_doc_path(
        self,
        conversation_id: str,
        skill_name: str,
        builtin_dir: Path | None,
    ) -> Path | None:
        workspace_dir = self._conversation_workspace(conversation_id)
        candidate = (workspace_dir / "skills" / skill_name / "SKILL.md").resolve()
        if candidate.exists():
            return candidate
        if builtin_dir:
            candidate = (builtin_dir / skill_name / "SKILL.md").resolve()
            if candidate.exists():
                return candidate
        return None

    def _collect_builtin_skills(
        self,
        conversation_id: str,
        skills_cfg: dict[str, Any],
        workspace_prefix: str,
    ) -> tuple[list[str], list[str], list[str]]:
        skill_paths: list[str] = []
        skill_tool_names: list[str] = []
        skills_mounts: list[str] = []

        if not skills_cfg.get("enabled", True):
            return skill_paths, skill_tool_names, skills_mounts

        chosen = self._resolve_builtin_skills_dir(skills_cfg)
        if not chosen:
            return skill_paths, skill_tool_names, skills_mounts
        mount_source, container_target = self._resolve_skill_mount(
            conversation_id,
            chosen,
            workspace_prefix,
        )
        skill_paths = [f"{container_target.rstrip('/')}/"]
        skills_mounts.append(f"{mount_source}:{container_target}:ro")
        for folder in chosen.iterdir():
            if folder.is_dir() and (folder / "SKILL.md").exists():
                skill_tool_names.append(folder.name)

        return skill_paths, skill_tool_names, skills_mounts

    def _ensure_conversation_skills(self, conversation_id: str) -> None:
        if conversation_id in self._conversation_skill_paths:
            return
        with SessionLocal() as db:
            conversation = db.get(Conversation, conversation_id)
            if not conversation:
                return
            self.prepare_conversation_skills(conversation_id, conversation.user_id, db=db)

    def prepare_conversation_skills(self, conversation_id: str, user_id: str, db: SessionLocal | None = None) -> list[str]:
        settings = get_settings()
        docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
        self._workspace_root = self._resolve_workspace_root(docker_cfg.get("workspace_root"), self._workspace_root)
        self._docker_workdir = str(docker_cfg.get("workdir", "/workspace"))
        skills_dir = self._conversation_skills_dir(conversation_id)
        workspace_dir = self._conversation_workspace(conversation_id)
        workspace_skills_dir = (workspace_dir / "skills").resolve()
        agentskills_root = Path(str(settings.skill_storage.agentskills_dir)).expanduser()
        if not agentskills_root.is_absolute():
            agentskills_root = (Path.cwd() / agentskills_root).resolve()
        agent_user_dir = (agentskills_root / user_id).resolve()
        if not str(agent_user_dir).startswith(str(agentskills_root.resolve())):
            raise ValueError("Invalid agent skills directory")
        agent_user_dir.mkdir(parents=True, exist_ok=True)
        self._conversation_agent_skills_dir[conversation_id] = agent_user_dir
        self._conversation_user_id[conversation_id] = user_id
        if self._container_runtime:
            mount_source = self._workspace_manager.agent_skills_mount_source(
                self._resolve_daemon_host(conversation_id),
                user_id,
                agent_user_dir,
            )
            workspace_prefix = "/" + str(self._docker_workdir).strip("/")
            skills_cfg = (settings.model_extra or {}).get("skills", {}) or {}
            _, _, skills_mounts = self._collect_builtin_skills(
                conversation_id,
                skills_cfg,
                workspace_prefix,
            )
            volumes: list[str] = [f"{mount_source}:{self._docker_workdir.rstrip('/')}/agent_skills:rw"]
            for entry in skills_mounts:
                if entry not in volumes:
                    volumes.append(entry)
            self._container_runtime.set_conversation_volumes(conversation_id, volumes)

        self._reset_dir_contents(skills_dir)
        self._reset_dir_contents(workspace_skills_dir)

        close_db = False
        user_skills: list[Skill] = []
        conversation_skills: list[Skill] = []
        installed_skills: list[Skill] = []
        if db is None:
            db = SessionLocal()
            close_db = True
        try:
            user = db.get(User, user_id)
            if not user:
                return []
            install_skill_ids = db.scalars(
                select(ConversationSkillInstall.skill_id).where(
                    ConversationSkillInstall.owner_user_id == user_id,
                    ConversationSkillInstall.conversation_id == conversation_id,
                )
            ).all()
            if install_skill_ids:
                installed_skills = db.scalars(
                    select(Skill).where(Skill.id.in_(install_skill_ids), Skill.status != "rejected")
                ).all()
            user_skills = db.scalars(
                select(Skill).where(
                    Skill.owner_user_id == user_id,
                    Skill.status.in_(("draft", "pending", "published")),
                    Skill.scope == "user",
                    Skill.source_type != "agent",
                )
            ).all()
            disabled_user_skill_ids = set(
                db.scalars(
                    select(ConversationSkillSetting.skill_id).where(
                        ConversationSkillSetting.owner_user_id == user_id,
                        ConversationSkillSetting.conversation_id == conversation_id,
                        ConversationSkillSetting.enabled.is_(False),
                    )
                ).all()
            )
            user_skills = [skill for skill in user_skills if skill.id not in disabled_user_skill_ids]
            conversation_skills = db.scalars(
                select(Skill).where(
                    Skill.owner_user_id == user_id,
                    Skill.status.in_(("draft", "pending", "published")),
                    Skill.scope == "conversation",
                    Skill.conversation_id == conversation_id,
                )
            ).all()
        finally:
            if close_db:
                db.close()

        repo_root = Path.cwd().resolve()
        published_dir_cfg = get_settings().skill_storage.skills_dir
        published_root = Path(str(published_dir_cfg)).expanduser()
        if not published_root.is_absolute():
            published_root = (repo_root / published_root).resolve()
        preskill_dir_cfg = get_settings().skill_storage.preskills_dir
        preskill_root = Path(str(preskill_dir_cfg)).expanduser()
        if not preskill_root.is_absolute():
            preskill_root = (repo_root / preskill_root).resolve()
        user_dir_cfg = get_settings().skill_storage.userskills_dir
        user_root = Path(str(user_dir_cfg)).expanduser()
        if not user_root.is_absolute():
            user_root = (repo_root / user_root).resolve()
        agentskill_dir_cfg = get_settings().skill_storage.agentskills_dir
        agentskill_root = Path(str(agentskill_dir_cfg)).expanduser()
        if not agentskill_root.is_absolute():
            agentskill_root = (repo_root / agentskill_root).resolve()

        tool_names: list[str] = []

        def copy_skill(src: Path, dest_name: str) -> None:
            if not src.exists() or not src.is_dir():
                return
            dest = skills_dir / dest_name
            if dest.exists():
                return
            shutil.copytree(src, dest)
            tool_names.append(dest_name)

        def source_dir_for_skill(skill: Skill) -> Path:
            if skill.source_type == "agent":
                return agentskill_root / skill.owner_user_id / skill.name
            if skill.scope == "conversation" and skill.conversation_id:
                return user_root / skill.owner_user_id / "_conversations" / skill.conversation_id / skill.name
            if skill.status == "published":
                return published_root / skill.name
            if skill.status == "pending":
                return preskill_root / skill.owner_user_id / skill.name
            return user_root / skill.owner_user_id / skill.name

        for skill in installed_skills:
            copy_skill(source_dir_for_skill(skill), skill.name)
        for skill in user_skills:
            copy_skill(source_dir_for_skill(skill), skill.name)
        for skill in conversation_skills:
            copy_skill(source_dir_for_skill(skill), skill.name)

        if skills_dir.exists():
            shutil.copytree(skills_dir, workspace_skills_dir, dirs_exist_ok=True)

        workspace_prefix = "/" + str(self._docker_workdir).strip("/")
        skill_paths = [f"{workspace_prefix}/skills/"] if tool_names else []

        self._conversation_skill_paths[conversation_id] = skill_paths
        self._conversation_skill_tool_names[conversation_id] = tool_names
        self._agents.pop(conversation_id, None)
        return tool_names
