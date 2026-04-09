from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class WorkspaceLayout:
    workspace_root: Path
    conversation_dir: Path
    uploads_dir: Path
    skills_dir: Path
    agent_skills_dir: Path
    workdir: str


class WorkspaceManager:
    def resolve_workspace_root(
        self,
        workspace_root_cfg: str | Path | None,
        default_root: Path,
    ) -> Path:
        if workspace_root_cfg is None:
            return default_root.resolve()
        workspace_root = Path(str(workspace_root_cfg)).expanduser()
        if not workspace_root.is_absolute():
            workspace_root = (Path.cwd() / workspace_root).resolve()
        return workspace_root

    def build_layout(
        self,
        conversation_id: str,
        workspace_root_cfg: str | Path | None,
        workdir: str = "/workspace",
        default_root: Path | None = None,
    ) -> WorkspaceLayout:
        base_root = default_root or (Path.cwd() / "workspaces")
        workspace_root = self.resolve_workspace_root(workspace_root_cfg, base_root)
        conversation_dir = (workspace_root / conversation_id).resolve()
        return WorkspaceLayout(
            workspace_root=workspace_root,
            conversation_dir=conversation_dir,
            uploads_dir=(conversation_dir / "uploads").resolve(),
            skills_dir=(conversation_dir / "skills").resolve(),
            agent_skills_dir=(conversation_dir / "agent_skills").resolve(),
            workdir="/" + str(workdir).strip("/"),
        )

    def agent_skills_mount_source(
        self,
        daemon_host: str,
        user_id: str,
        agent_user_dir: Path,
    ) -> str:
        if daemon_host:
            return f"/agent_skills/{user_id}"
        return str(agent_user_dir)
