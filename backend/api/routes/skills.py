from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import shutil
import re
import zipfile
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
import yaml
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user, require_admin
from backend.core.config import get_settings
from backend.core.database import get_db
from backend.core.models import (
    Conversation,
    ConversationSkillInstall,
    ConversationSkillSetting,
    ConversationMessage,
    Skill,
    SkillAuditLog,
    SkillGroup,
    SkillGroupSkill,
    SkillGroupUser,
    User,
)
from backend.security import can_edit_skill_resource, can_view_skill_resource, require_allowed
from backend.services.deepagents_service import deepagent_service

router = APIRouter(prefix="/skills", tags=["skills"])

SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")

ROOT_DIR = Path.cwd()


def _resolve_storage_path(path_str: str) -> Path:
    path = Path(str(path_str)).expanduser()
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    return path


def _builtin_skill_names() -> set[str]:
    settings = get_settings()
    extra = settings.model_extra or {}
    skills_cfg = extra.get("skills", {}) or {}
    if not skills_cfg.get("enabled", True):
        return set()
    source_dirs = skills_cfg.get("directories", []) or []
    resolved: list[Path] = []
    for entry in source_dirs:
        path = Path(str(entry)).expanduser()
        if not path.is_absolute():
            path = (ROOT_DIR / path).resolve()
        resolved.append(path)
    valid = [p for p in resolved if p.exists() and p.is_dir()]
    if not valid:
        return set()
    chosen = valid[-1]
    names: set[str] = set()
    for folder in chosen.iterdir():
        if folder.is_dir() and (folder / "SKILL.md").exists():
            names.add(folder.name)
    return names


def _builtin_skills() -> list[dict]:
    settings = get_settings()
    extra = settings.model_extra or {}
    skills_cfg = extra.get("skills", {}) or {}
    if not skills_cfg.get("enabled", True):
        return []
    source_dirs = skills_cfg.get("directories", []) or []
    resolved: list[Path] = []
    for entry in source_dirs:
        path = Path(str(entry)).expanduser()
        if not path.is_absolute():
            path = (ROOT_DIR / path).resolve()
        resolved.append(path)
    valid = [p for p in resolved if p.exists() and p.is_dir()]
    if not valid:
        return []
    chosen = valid[-1]
    items: list[dict] = []
    for folder in sorted(chosen.iterdir(), key=lambda p: p.name):
        if not folder.is_dir():
            continue
        skill_md = folder / "SKILL.md"
        if not skill_md.exists():
            continue
        meta = _read_skill_frontmatter(skill_md)
        name = meta.get("name") or folder.name
        description = meta.get("description") or ""
        display_name = meta.get("display_name") or meta.get("title") or ""
        items.append(
            {
                "id": f"builtin:{name}",
                "name": str(name),
                "display_name": str(display_name) if display_name else None,
                "description": str(description) if description else None,
                "source_type": "builtin",
                "scope": "global",
            }
        )
    return items


def _resolve_builtin_skill(name: str) -> tuple[dict, Path] | None:
    settings = get_settings()
    extra = settings.model_extra or {}
    skills_cfg = extra.get("skills", {}) or {}
    if not skills_cfg.get("enabled", True):
        return None
    source_dirs = skills_cfg.get("directories", []) or []
    resolved: list[Path] = []
    for entry in source_dirs:
        path = Path(str(entry)).expanduser()
        if not path.is_absolute():
            path = (ROOT_DIR / path).resolve()
        resolved.append(path)
    valid = [p for p in resolved if p.exists() and p.is_dir()]
    if not valid:
        return None
    chosen = valid[-1]
    for folder in sorted(chosen.iterdir(), key=lambda p: p.name):
        if not folder.is_dir():
            continue
        skill_md = folder / "SKILL.md"
        if not skill_md.exists():
            continue
        meta = _read_skill_frontmatter(skill_md)
        builtin_name = str(meta.get("name") or folder.name)
        if builtin_name == name:
            return (
                {
                    "name": builtin_name,
                    "display_name": str(meta.get("display_name") or meta.get("title") or "") or None,
                    "description": str(meta.get("description") or "") or None,
                },
                folder,
            )
    return None


_storage = get_settings().skill_storage
USERSKILLS_DIR = _resolve_storage_path(_storage.userskills_dir)
PRESKILLS_DIR = _resolve_storage_path(_storage.preskills_dir)
PUBLISHED_DIR = _resolve_storage_path(_storage.skills_dir)
AGENTSKILLS_DIR = _resolve_storage_path(_storage.agentskills_dir)


class CreateSkillRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    conversation_id: str | None = None


class UpdateSkillRequest(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    is_public: bool | None = None
    is_public_edit: bool | None = None
    scope: str | None = None
    conversation_id: str | None = None


class PublishSkillRequest(BaseModel):
    comment: str | None = None


class RejectSkillRequest(BaseModel):
    comment: str | None = None


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None


class AddSkillToGroupRequest(BaseModel):
    skill_id: str


class AddUserToGroupRequest(BaseModel):
    user_id: str


class FileWriteRequest(BaseModel):
    path: str
    content: str | None = ""


class DirCreateRequest(BaseModel):
    path: str


class RenameRequest(BaseModel):
    from_path: str
    to_path: str


class InstallSkillRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=36)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _invalidate_user_skill_agents(db: Session, user_id: str) -> None:
    deepagent_service.invalidate_user_conversations(user_id, db=db)


def _serialize_skill(skill: Skill) -> dict:
    return {
        "id": skill.id,
        "owner_user_id": skill.owner_user_id,
        "source_type": skill.source_type,
        "status": skill.status,
        "scope": skill.scope,
        "group_id": skill.group_id,
        "conversation_id": skill.conversation_id,
        "name": skill.name,
        "display_name": skill.display_name,
        "description": skill.description,
        "is_public": bool(skill.is_public),
        "is_public_edit": bool(skill.is_public_edit),
        "usage_count": int(skill.usage_count or 0),
        "cloned_from_skill_id": skill.cloned_from_skill_id,
        "pending_comment": skill.pending_comment,
        "published_at": skill.published_at.isoformat() if skill.published_at else None,
        "published_by": skill.published_by,
        "rejected_at": skill.rejected_at.isoformat() if skill.rejected_at else None,
        "rejected_by": skill.rejected_by,
        "rejected_reason": skill.rejected_reason,
        "created_at": skill.created_at.isoformat() if skill.created_at else None,
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }


def _serialize_group(group: SkillGroup) -> dict:
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "created_by": group.created_by,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "updated_at": group.updated_at.isoformat() if group.updated_at else None,
    }


def _serialize_skill_install(install: ConversationSkillInstall, conversation: Conversation | None = None) -> dict:
    return {
        "id": install.id,
        "owner_user_id": install.owner_user_id,
        "conversation_id": install.conversation_id,
        "conversation_title": conversation.title if conversation else None,
        "skill_id": install.skill_id,
        "installed_by_user_id": install.installed_by_user_id,
        "created_at": install.created_at.isoformat() if install.created_at else None,
        "updated_at": install.updated_at.isoformat() if install.updated_at else None,
    }


def _skill_dir(skill: Skill) -> Path:
    if skill.scope == "conversation" and skill.conversation_id:
        return USERSKILLS_DIR / skill.owner_user_id / "_conversations" / skill.conversation_id / skill.name
    if skill.status == "published":
        return PUBLISHED_DIR / skill.name
    if skill.status == "pending":
        return PRESKILLS_DIR / skill.owner_user_id / skill.name
    if skill.source_type == "agent":
        return AGENTSKILLS_DIR / skill.owner_user_id / skill.name
    return USERSKILLS_DIR / skill.owner_user_id / skill.name


def _get_install_rows(db: Session, skill_ids: list[str], owner_user_id: str | None = None) -> tuple[list[ConversationSkillInstall], dict[str, Conversation]]:
    if not skill_ids:
        return [], {}
    stmt = select(ConversationSkillInstall).where(ConversationSkillInstall.skill_id.in_(skill_ids))
    if owner_user_id:
        stmt = stmt.where(ConversationSkillInstall.owner_user_id == owner_user_id)
    install_rows = db.scalars(stmt.order_by(desc(ConversationSkillInstall.updated_at))).all()
    conversation_ids = list({row.conversation_id for row in install_rows})
    conversation_rows = (
        db.scalars(select(Conversation).where(Conversation.id.in_(conversation_ids))).all()
        if conversation_ids
        else []
    )
    conversation_map = {row.id: row for row in conversation_rows}
    return install_rows, conversation_map


def _skill_market_item(
    skill: Skill,
    installs: list[ConversationSkillInstall] | None = None,
    conversation_map: dict[str, Conversation] | None = None,
) -> dict:
    item = _serialize_skill(skill)
    installs = installs or []
    conversation_map = conversation_map or {}
    item["install_count"] = len(installs)
    item["installs"] = [_serialize_skill_install(row, conversation_map.get(row.conversation_id)) for row in installs]
    item["agent_relation"] = {
        "type": "conversation",
        "label": "conversation",
        "count": len(installs),
    }
    return item


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _ensure_skill_scaffold(skill: Skill) -> None:
    skill_dir = _skill_dir(skill)
    _ensure_dir(skill_dir)
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        title = skill.display_name or skill.name
        content = f"---\nname: {skill.name}\ndescription: \"\"\nlicense: MIT\ncompatibility: designed for deepagents-cli\n---\n\n# {title}\n"
        skill_md.write_text(content, encoding="utf-8")


def _read_skill_frontmatter(skill_md: Path) -> dict:
    if not skill_md.exists():
        return {}
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    raw = parts[1]
    try:
        data = yaml.safe_load(raw) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _update_skill_frontmatter_name(skill_md: Path, new_name: str) -> None:
    if not skill_md.exists():
        return
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception:
        return
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            meta = yaml.safe_load(parts[1]) or {}
            if not isinstance(meta, dict):
                meta = {}
            meta["name"] = new_name
            front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
            rebuilt = f"---\n{front}\n---{parts[2]}"
            skill_md.write_text(rebuilt, encoding="utf-8")
            return
    # Fallback: prepend frontmatter if missing
    front = f"---\nname: {new_name}\ndescription: \"\"\nlicense: MIT\ncompatibility: designed for deepagents-cli\n---\n\n"
    skill_md.write_text(front + text, encoding="utf-8")


def _normalize_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed or not SKILL_NAME_RE.fullmatch(trimmed):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Skill name must be english letters, numbers, or hyphen")
    return trimmed


def _suggest_skill_name(raw_name: str) -> str:
    base = Path(raw_name).stem.strip().lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", base)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or f"skill-{uuid4().hex[:6]}"


def _name_exists_for_owner(db: Session, owner_id: str, name: str, exclude_id: str | None = None) -> bool:
    stmt = select(Skill.id).where(Skill.owner_user_id == owner_id, Skill.name == name)
    if exclude_id:
        stmt = stmt.where(Skill.id != exclude_id)
    return db.scalar(stmt) is not None


def _userskill_dir_exists(owner_id: str, name: str) -> bool:
    return (USERSKILLS_DIR / owner_id / name).exists()


def _name_exists_globally(db: Session, name: str, statuses: tuple[str, ...] = ("pending", "published"), exclude_id: str | None = None) -> bool:
    stmt = select(Skill.id).where(Skill.name == name, Skill.status.in_(statuses))
    if exclude_id:
        stmt = stmt.where(Skill.id != exclude_id)
    return db.scalar(stmt) is not None


def _unique_name(base: str, exists_fn) -> str:
    if not exists_fn(base):
        return base
    for _ in range(50):
        suffix = uuid4().hex[:6]
        candidate = f"{base}-{suffix}"
        if not exists_fn(candidate):
            return candidate
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Unable to generate unique skill name")


def _move_dir(src: Path, dest: Path) -> None:
    if not src.exists():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Skill directory missing")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Target skill directory already exists")
    shutil.move(str(src), str(dest))


def _copy_dir(src: Path, dest: Path) -> None:
    if not src.exists():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Skill directory missing")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Target skill directory already exists")
    shutil.copytree(src, dest)


def _resolve_path(root: Path, rel_path: str) -> Path:
    if not rel_path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Path is required")
    rel = Path(rel_path)
    if rel.is_absolute():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Absolute path is not allowed")
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if root_resolved != target and root_resolved not in target.parents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path")
    return target


def _extract_zip_to_skill_root(raw_bytes: bytes, root: Path) -> None:
    try:
        archive = zipfile.ZipFile(BytesIO(raw_bytes))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid zip archive") from exc

    members = [info for info in archive.infolist() if info.filename and not info.is_dir()]
    if not members:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Zip archive is empty")

    member_parts = [Path(info.filename).parts for info in members if Path(info.filename).parts]
    top_level_parts = {parts[0] for parts in member_parts}
    strip_root = len(top_level_parts) == 1 and all(len(parts) > 1 for parts in member_parts)
    skill_md_found = False

    for info in members:
        rel_parts = list(Path(info.filename).parts)
        if strip_root and len(rel_parts) > 1:
            rel_parts = rel_parts[1:]
        elif strip_root and len(rel_parts) == 1:
            continue
        rel_path = Path(*rel_parts).as_posix()
        target = _resolve_path(root, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as source:
            target.write_bytes(source.read())
        if target.name == "SKILL.md":
            skill_md_found = True

    if not skill_md_found:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Zip archive must contain SKILL.md")


def _can_view_skill(db: Session, user: User, skill: Skill) -> bool:
    return can_view_skill_resource(db, user, skill).allowed


def _require_view_skill(db: Session, user: User, skill: Skill) -> None:
    require_allowed(can_view_skill_resource(db, user, skill))


def _can_edit_skill(user: User, skill: Skill) -> bool:
    return can_edit_skill_resource(user, skill).allowed


def _require_edit_skill(user: User, skill: Skill) -> None:
    require_allowed(can_edit_skill_resource(user, skill))


def _log(db: Session, skill_id: str, actor_id: str, action: str, detail: dict | None = None) -> None:
    log = SkillAuditLog(
        id=str(uuid4()),
        skill_id=skill_id,
        action=action,
        actor_user_id=actor_id,
        detail=detail or {},
        created_at=_now(),
    )
    db.add(log)


@router.get("/mine")
def list_my_skills(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    skills = db.scalars(
        select(Skill)
        .where(Skill.owner_user_id == current_user.id, Skill.source_type != "agent")
        .order_by(Skill.created_at.desc())
    ).all()
    return {"items": [_serialize_skill(s) for s in skills]}


@router.get("/agents")
def list_agent_skills(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    skills = db.scalars(
        select(Skill)
        .where(Skill.owner_user_id == current_user.id, Skill.source_type == "agent")
        .order_by(Skill.created_at.desc())
    ).all()
    return {"items": [_serialize_skill(s) for s in skills]}


@router.get("/agent_skills")
def list_agent_skill_dirs(current_user: User = Depends(get_current_user)) -> dict:
    root = AGENTSKILLS_DIR / current_user.id
    if not root.exists() or not root.is_dir():
        return {"items": []}
    items: list[dict] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        meta = _read_skill_frontmatter(skill_md)
        name = str(meta.get("name") or child.name)
        items.append(
            {
                "name": name,
                "dir_name": child.name,
                "display_name": meta.get("display_name") or meta.get("title") or None,
                "description": meta.get("description") or None,
            }
        )
    return {"items": items}


@router.post("/agent_skills/{skill_name}/move_to_user")
def move_agent_skill_to_user(
    skill_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    name = _normalize_name(skill_name)
    source_dir = AGENTSKILLS_DIR / current_user.id / name
    if not source_dir.exists():
        raise HTTPException(status_code=404, detail="Agent skill not found")
    new_name = _unique_name(
        name,
        lambda n: _name_exists_for_owner(db, current_user.id, n) or _userskill_dir_exists(current_user.id, n),
    )
    target_dir = USERSKILLS_DIR / current_user.id / new_name
    _ensure_dir(target_dir.parent)
    if new_name != name:
        _update_skill_frontmatter_name(source_dir / "SKILL.md", new_name)
    shutil.move(str(source_dir), str(target_dir))
    skill = Skill(
        id=str(uuid4()),
        owner_user_id=current_user.id,
        source_type="user",
        status="draft",
        scope="user",
        group_id=None,
        conversation_id=None,
        name=new_name,
        display_name=None,
        description=None,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return {"item": _serialize_skill(skill)}


@router.post("/{skill_id}/move_to_agent")
def move_user_skill_to_agent(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill or skill.owner_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.status != "draft":
        raise HTTPException(status_code=400, detail="Only draft skills can be moved")
    if skill.source_type == "agent":
        return {"item": _serialize_skill(skill)}
    source_dir = _skill_dir(skill)
    if not source_dir.exists():
        raise HTTPException(status_code=404, detail="Skill directory not found")
    target_dir = AGENTSKILLS_DIR / current_user.id / skill.name
    _ensure_dir(target_dir.parent)
    new_name = skill.name
    if target_dir.exists():
        new_name = _unique_name(
            skill.name,
            lambda n: (AGENTSKILLS_DIR / current_user.id / n).exists(),
        )
        target_dir = AGENTSKILLS_DIR / current_user.id / new_name
        _update_skill_frontmatter_name(source_dir / "SKILL.md", new_name)
        skill.name = new_name
    shutil.move(str(source_dir), str(target_dir))
    skill.source_type = "agent"
    db.commit()
    db.refresh(skill)
    return {"item": _serialize_skill(skill)}


@router.get("/all")
def list_all_skills(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    skills = db.scalars(select(Skill).order_by(Skill.created_at.desc())).all()
    return {"items": [_serialize_skill(s) for s in skills]}


@router.get("/pending")
def list_pending_skills(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    skills = db.scalars(select(Skill).where(Skill.status == "pending").order_by(Skill.created_at.desc())).all()
    return {"items": [_serialize_skill(s) for s in skills]}


@router.get("/published")
def list_published_skills(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if current_user.is_admin:
        skills = db.scalars(select(Skill).where(Skill.status == "published", Skill.scope.in_(("global", "group")))).all()
        return {"items": [_serialize_skill(s) for s in skills]}
    subq = (
        select(SkillGroupSkill.skill_id)
        .join(SkillGroupUser, SkillGroupSkill.group_id == SkillGroupUser.group_id)
        .where(SkillGroupUser.user_id == current_user.id)
    )
    skills = db.scalars(
        select(Skill).where(
            Skill.status == "published",
            or_(Skill.scope == "global", Skill.id.in_(subq)),
        )
    ).all()
    return {"items": [_serialize_skill(s) for s in skills]}


@router.get("/publish_requests")
def list_publish_requests(
    status: str = "pending",
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    if status != "pending":
        return {"items": []}
    skills = db.scalars(select(Skill).where(Skill.status == "pending").order_by(Skill.created_at.desc())).all()
    items = [
        {
            "id": s.id,
            "skill_id": s.id,
            "requester_user_id": s.owner_user_id,
            "comment": s.pending_comment,
        }
        for s in skills
    ]
    return {"items": items}


@router.get("/builtin")
def list_builtin_skills(current_user: User = Depends(get_current_user)) -> dict:
    return {"items": _builtin_skills()}


@router.get("/marketplace")
def get_skill_marketplace(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    published_rows = list_published_skills(current_user=current_user, db=db)["items"]
    published_ids = [str(item["id"]) for item in published_rows if item.get("id")]
    published_models = db.scalars(select(Skill).where(Skill.id.in_(published_ids))).all() if published_ids else []
    published_map = {row.id: row for row in published_models}
    published_install_rows, published_conversation_map = _get_install_rows(db, published_ids, owner_user_id=current_user.id)
    installs_by_skill: dict[str, list[ConversationSkillInstall]] = {}
    for row in published_install_rows:
        installs_by_skill.setdefault(row.skill_id, []).append(row)

    my_models = db.scalars(
        select(Skill)
        .where(
            Skill.owner_user_id == current_user.id,
            Skill.source_type != "agent",
            Skill.status != "rejected",
        )
        .order_by(desc(Skill.updated_at))
    ).all()
    my_ids = [row.id for row in my_models]
    my_install_rows, my_conversation_map = _get_install_rows(db, my_ids, owner_user_id=current_user.id)
    my_installs_by_skill: dict[str, list[ConversationSkillInstall]] = {}
    for row in my_install_rows:
        my_installs_by_skill.setdefault(row.skill_id, []).append(row)

    conversations = db.scalars(
        select(Conversation)
        .where(Conversation.user_id == current_user.id)
        .order_by(desc(Conversation.updated_at))
        .limit(50)
    ).all()

    return {
        "published": [
            _skill_market_item(
                published_map[item["id"]],
                installs_by_skill.get(item["id"], []),
                published_conversation_map,
            )
            for item in published_rows
            if item.get("id") in published_map
        ],
        "builtin": _builtin_skills(),
        "my_skills": [
            _skill_market_item(row, my_installs_by_skill.get(row.id, []), my_conversation_map)
            for row in my_models
        ],
        "conversations": [
            {
                "id": row.id,
                "title": row.title,
                "model": row.model_name,
                "execution_backend": getattr(row, "execution_backend", "deepagents"),
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in conversations
        ],
    }


@router.post("")
def create_skill(
    payload: CreateSkillRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    name = _normalize_name(payload.name)
    if _name_exists_for_owner(db, current_user.id, name) or _userskill_dir_exists(current_user.id, name):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill name already exists")
    conversation_id = (payload.conversation_id or "").strip() or None
    scope = "user"
    if conversation_id:
        conversation = db.get(Conversation, conversation_id)
        if not conversation or conversation.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
        scope = "conversation"
    skill = Skill(
        id=str(uuid4()),
        owner_user_id=current_user.id,
        source_type="user",
        status="draft",
        scope=scope,
        group_id=None,
        conversation_id=conversation_id,
        name=name,
        display_name=payload.display_name or None,
        description=payload.description or None,
        is_public=False,
        is_public_edit=False,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(skill)
    _log(db, skill.id, current_user.id, "create")
    db.commit()
    _ensure_skill_scaffold(skill)
    _invalidate_user_skill_agents(db, current_user.id)
    return {"item": _serialize_skill(skill)}


@router.post("/import")
def import_skill(
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
    display_name: str | None = Form(default=None),
    description: str | None = Form(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    requested_name = name.strip() if name else ""
    normalized_name = _normalize_name(requested_name or _suggest_skill_name(file.filename or "skill"))
    if _name_exists_for_owner(db, current_user.id, normalized_name) or _userskill_dir_exists(current_user.id, normalized_name):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill name already exists")

    skill = Skill(
        owner_user_id=current_user.id,
        source_type="user",
        status="draft",
        scope="user",
        name=normalized_name,
        display_name=(display_name or "").strip() or None,
        description=(description or "").strip() or None,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)

    skill_dir = _skill_dir(skill)
    _ensure_dir(skill_dir)
    raw_bytes = file.file.read()
    suffix = Path(file.filename or "").suffix.lower()

    try:
        if suffix == ".zip":
            _extract_zip_to_skill_root(raw_bytes, skill_dir)
        else:
            content = raw_bytes.decode("utf-8", errors="replace")
            title = skill.display_name or skill.name
            if suffix in {".md", ".txt"}:
                text = content
            else:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only .zip, .md, or .txt files are supported")
            if not text.strip().startswith("---"):
                text = (
                    f"---\nname: {skill.name}\ndescription: \"{skill.description or ''}\"\nlicense: MIT\n"
                    f"compatibility: designed for deepagents-cli\n---\n\n# {title}\n\n{text}"
                )
            (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
        _update_skill_frontmatter_name(skill_dir / "SKILL.md", skill.name)
    except Exception as exc:  # noqa: BLE001
        if skill_dir.exists():
            shutil.rmtree(skill_dir, ignore_errors=True)
        db.delete(skill)
        db.commit()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    _log(
        db,
        skill.id,
        current_user.id,
        "import",
        {"filename": file.filename, "content_type": file.content_type},
    )
    db.commit()
    db.refresh(skill)
    _invalidate_user_skill_agents(db, current_user.id)
    return {"item": _serialize_skill(skill)}


@router.get("/usage")
def list_skill_usage(
    conversation_id: str | None = None,
    include_non_skills: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    builtin_names = _builtin_skill_names()
    skill_rows = db.scalars(select(Skill)).all()
    skill_by_name: dict[str, Skill] = {}
    for skill in skill_rows:
        if skill.name not in skill_by_name:
            skill_by_name[skill.name] = skill
    skill_name_set = set(skill_by_name.keys()) | builtin_names
    non_skill_tools = {
        "terminal",
        "read_file",
        "write_file",
        "edit_file",
        "ls",
        "glob",
        "web_search",
        "internet_search",
        "fetch_url",
    }

    if conversation_id:
        conversation = db.get(Conversation, conversation_id)
        if not conversation or conversation.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Conversation not found")

    items: list[dict] = []
    if conversation_id:
        query = (
            select(ConversationMessage.tool_name, func.count(ConversationMessage.id))
            .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
            .where(Conversation.user_id == current_user.id)
            .where(ConversationMessage.message_type == "ToolMessage")
            .where(ConversationMessage.tool_name.isnot(None))
            .where(ConversationMessage.conversation_id == conversation_id)
        )
        if not include_non_skills:
            query = query.where(~ConversationMessage.tool_name.in_(sorted(non_skill_tools)))
        query = query.group_by(ConversationMessage.tool_name).order_by(desc(func.count(ConversationMessage.id)))
        rows = db.execute(query).all()
        for tool_name, count in rows:
            name = str(tool_name)
            item = {"tool_name": name, "count": int(count)}
            if name in builtin_names:
                item["source"] = "builtin"
            elif name in skill_by_name:
                skill = skill_by_name[name]
                item["source"] = skill.source_type
                item["status"] = skill.status
                if skill.display_name:
                    item["display_name"] = skill.display_name
            else:
                item["source"] = "tool"
            items.append(item)
        return {"items": items}

    for skill in skill_rows:
        item = {"tool_name": skill.name, "count": int(skill.usage_count or 0), "source": skill.source_type}
        if skill.status:
            item["status"] = skill.status
        if skill.display_name:
            item["display_name"] = skill.display_name
        items.append(item)

    if builtin_names:
        builtin_query = (
            select(ConversationMessage.tool_name, func.count(ConversationMessage.id))
            .where(ConversationMessage.message_type == "ToolMessage")
            .where(ConversationMessage.tool_name.in_(sorted(builtin_names)))
            .group_by(ConversationMessage.tool_name)
        )
        for tool_name, count in db.execute(builtin_query).all():
            items.append({"tool_name": str(tool_name), "count": int(count), "source": "builtin"})

    if include_non_skills:
        extra_query = (
            select(ConversationMessage.tool_name, func.count(ConversationMessage.id))
            .where(ConversationMessage.message_type == "ToolMessage")
            .where(ConversationMessage.tool_name.isnot(None))
            .where(~ConversationMessage.tool_name.in_(sorted(skill_name_set)))
            .group_by(ConversationMessage.tool_name)
        )
        for tool_name, count in db.execute(extra_query).all():
            items.append({"tool_name": str(tool_name), "count": int(count), "source": "tool"})

    return {"items": items}


@router.get("/{skill_id}")
def get_skill(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    _require_view_skill(db, current_user, skill)
    return {"item": _serialize_skill(skill)}


@router.get("/{skill_id}/installs")
def list_skill_installs(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    _require_view_skill(db, current_user, skill)
    install_rows = db.scalars(
        select(ConversationSkillInstall)
        .where(
            ConversationSkillInstall.skill_id == skill_id,
            ConversationSkillInstall.owner_user_id == current_user.id,
        )
        .order_by(desc(ConversationSkillInstall.updated_at))
    ).all()
    conversation_ids = [row.conversation_id for row in install_rows]
    conversations = (
        db.scalars(select(Conversation).where(Conversation.id.in_(conversation_ids))).all()
        if conversation_ids
        else []
    )
    conversation_map = {row.id: row for row in conversations if row.user_id == current_user.id}
    visible_rows = [row for row in install_rows if row.conversation_id in conversation_map]
    return {"items": [_serialize_skill_install(row, conversation_map.get(row.conversation_id)) for row in visible_rows]}


@router.post("/{skill_id}/install")
def install_skill_to_conversation(
    skill_id: str,
    payload: InstallSkillRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    _require_view_skill(db, current_user, skill)
    if skill.status == "rejected":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Rejected skill cannot be installed")
    conversation = db.get(Conversation, payload.conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    existing = db.scalar(
        select(ConversationSkillInstall).where(
            ConversationSkillInstall.owner_user_id == current_user.id,
            ConversationSkillInstall.conversation_id == conversation.id,
            ConversationSkillInstall.skill_id == skill.id,
        )
    )
    if skill.owner_user_id == current_user.id and skill.scope == "user":
        setting = db.scalar(
            select(ConversationSkillSetting).where(
                ConversationSkillSetting.owner_user_id == current_user.id,
                ConversationSkillSetting.conversation_id == conversation.id,
                ConversationSkillSetting.skill_id == skill.id,
            )
        )
        if setting:
            setting.enabled = True
            setting.updated_by_user_id = current_user.id
            setting.updated_at = _now()
            db.add(setting)
        else:
            db.add(
                ConversationSkillSetting(
                    id=str(uuid4()),
                    owner_user_id=current_user.id,
                    conversation_id=conversation.id,
                    skill_id=skill.id,
                    enabled=True,
                    updated_by_user_id=current_user.id,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        _log(db, skill.id, current_user.id, "conversation_enable", {"conversation_id": conversation.id})
        db.commit()
        deepagent_service.invalidate_conversation(conversation.id)
        return {"enabled": True, "conversation_id": conversation.id, "skill_id": skill.id}
    if existing:
        return {"item": _serialize_skill_install(existing, conversation)}
    row = ConversationSkillInstall(
        id=str(uuid4()),
        owner_user_id=current_user.id,
        conversation_id=conversation.id,
        skill_id=skill.id,
        installed_by_user_id=current_user.id,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(row)
    _log(db, skill.id, current_user.id, "install", {"conversation_id": conversation.id})
    db.commit()
    db.refresh(row)
    deepagent_service.invalidate_conversation(conversation.id)
    return {"item": _serialize_skill_install(row, conversation)}


@router.delete("/{skill_id}/install/{conversation_id}")
def uninstall_skill_from_conversation(
    skill_id: str,
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    row = db.scalar(
        select(ConversationSkillInstall).where(
            ConversationSkillInstall.owner_user_id == current_user.id,
            ConversationSkillInstall.conversation_id == conversation_id,
            ConversationSkillInstall.skill_id == skill_id,
        )
    )
    if skill.owner_user_id == current_user.id and skill.scope == "user":
        setting = db.scalar(
            select(ConversationSkillSetting).where(
                ConversationSkillSetting.owner_user_id == current_user.id,
                ConversationSkillSetting.conversation_id == conversation_id,
                ConversationSkillSetting.skill_id == skill_id,
            )
        )
        if setting:
            setting.enabled = False
            setting.updated_by_user_id = current_user.id
            setting.updated_at = _now()
            db.add(setting)
        else:
            db.add(
                ConversationSkillSetting(
                    id=str(uuid4()),
                    owner_user_id=current_user.id,
                    conversation_id=conversation_id,
                    skill_id=skill_id,
                    enabled=False,
                    updated_by_user_id=current_user.id,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        _log(db, skill.id, current_user.id, "conversation_disable", {"conversation_id": conversation_id})
        db.commit()
        deepagent_service.invalidate_conversation(conversation_id)
        return {"enabled": False, "conversation_id": conversation_id, "skill_id": skill_id}
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Install relation not found")
    db.delete(row)
    _log(db, skill.id, current_user.id, "uninstall", {"conversation_id": conversation_id})
    db.commit()
    deepagent_service.invalidate_conversation(conversation_id)
    return {"message": "deleted"}


@router.patch("/{skill_id}")
def update_skill(
    skill_id: str,
    payload: UpdateSkillRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if not _can_edit_skill(current_user, skill) and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")

    updates = payload.model_dump(exclude_none=True)
    renamed = False
    if "name" in updates:
        if skill.status == "published":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Published skill cannot be renamed")
        name = _normalize_name(updates["name"])
        if skill.status in {"pending", "published"}:
            if _name_exists_globally(db, name, exclude_id=skill.id):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill name already exists")
        else:
            if _name_exists_for_owner(db, skill.owner_user_id, name, exclude_id=skill.id) or _userskill_dir_exists(
                skill.owner_user_id, name
            ):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill name already exists")
        if name != skill.name:
            old_dir = _skill_dir(skill)
            skill.name = name
            renamed = True
            new_dir = _skill_dir(skill)
            _move_dir(old_dir, new_dir)

    if "display_name" in updates:
        skill.display_name = updates["display_name"]
    if "description" in updates:
        skill.description = updates["description"]

    is_public_requested = "is_public" in updates
    if is_public_requested:
        if not current_user.is_admin or skill.status != "published":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
        skill.is_public = bool(updates["is_public"])
        skill.scope = "global" if skill.is_public else "user"
        if skill.is_public:
            skill.group_id = None
        if not skill.is_public:
            skill.is_public_edit = False
    if "scope" in updates or "conversation_id" in updates:
        requested_scope = str(updates.get("scope") or skill.scope or "").strip()
        requested_conversation_id = updates.get("conversation_id", skill.conversation_id)
        if not current_user.is_admin:
            if skill.owner_user_id != current_user.id or skill.status == "published":
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
            if requested_scope not in {"user", "conversation"}:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
        else:
            if requested_scope not in {"user", "global", "group", "conversation"}:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid scope")
        if requested_scope == "conversation":
            requested_conversation_id = str(requested_conversation_id or "").strip()
            if not requested_conversation_id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="conversation_id is required for conversation scope")
            conversation = db.get(Conversation, requested_conversation_id)
            if not conversation or conversation.user_id != skill.owner_user_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
            skill.conversation_id = requested_conversation_id
        else:
            skill.conversation_id = None
        skill.scope = requested_scope
        if requested_scope != "group":
            skill.group_id = None
        skill.is_public = requested_scope == "global"
    elif "conversation_id" in updates and updates["conversation_id"] is None:
        skill.conversation_id = None
    if "scope" in updates and "conversation_id" not in updates and skill.scope == "conversation" and not skill.conversation_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="conversation_id is required for conversation scope")
    if "is_public_edit" in updates:
        if not current_user.is_admin or skill.status != "published" or not skill.is_public:
            # Allow turning off public edit when public is being disabled in the same request.
            if not (is_public_requested and updates.get("is_public") is False):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
        skill.is_public_edit = bool(updates["is_public_edit"])

    skill.updated_at = _now()
    db.add(skill)
    _log(db, skill.id, current_user.id, "edit", {"renamed": renamed})
    db.commit()
    db.refresh(skill)
    _invalidate_user_skill_agents(db, skill.owner_user_id)
    return {"item": _serialize_skill(skill)}


@router.delete("/{skill_id}")
def delete_skill(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if not current_user.is_admin:
        if skill.owner_user_id != current_user.id or skill.status not in {"draft", "rejected"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    skill_dir = _skill_dir(skill)
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    db.query(ConversationSkillInstall).filter(ConversationSkillInstall.skill_id == skill_id).delete()
    db.query(ConversationSkillSetting).filter(ConversationSkillSetting.skill_id == skill_id).delete()
    db.delete(skill)
    _log(db, skill_id, current_user.id, "delete")
    db.commit()
    _invalidate_user_skill_agents(db, skill.owner_user_id)
    return {"message": "deleted"}


@router.post("/{skill_id}/publish")
def request_publish(
    skill_id: str,
    payload: PublishSkillRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if skill.owner_user_id != current_user.id or skill.source_type == "agent":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    if skill.status not in {"draft", "rejected"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Skill cannot be published")
    skill_dir = _skill_dir(skill)
    if not (skill_dir / "SKILL.md").exists():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SKILL.md is required")

    def exists_fn(candidate: str) -> bool:
        return _name_exists_globally(db, candidate, exclude_id=skill.id)

    if exists_fn(skill.name):
        new_name = _unique_name(skill.name, exists_fn)
        old_dir = _skill_dir(skill)
        skill.name = new_name
        new_dir = _skill_dir(skill)
        _move_dir(old_dir, new_dir)

    old_dir = _skill_dir(skill)
    skill.status = "pending"
    skill.scope = "user"
    skill.group_id = None
    skill.pending_comment = payload.comment
    skill.rejected_reason = None
    skill.rejected_at = None
    skill.rejected_by = None
    skill.updated_at = _now()
    new_dir = _skill_dir(skill)
    _move_dir(old_dir, new_dir)
    _log(db, skill.id, current_user.id, "submit", {"comment": payload.comment})
    db.commit()
    db.refresh(skill)
    return {"item": _serialize_skill(skill)}


@router.post("/{skill_id}/withdraw")
def withdraw_publish(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if skill.owner_user_id != current_user.id or skill.status != "pending":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    old_dir = _skill_dir(skill)
    skill.status = "draft"
    skill.scope = "user"
    skill.group_id = None
    skill.pending_comment = None
    skill.updated_at = _now()
    new_dir = _skill_dir(skill)
    _move_dir(old_dir, new_dir)
    _log(db, skill.id, current_user.id, "withdraw")
    db.commit()
    db.refresh(skill)
    return {"item": _serialize_skill(skill)}


@router.post("/publish_requests/{skill_id}/approve")
def approve_publish(
    skill_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if skill.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Skill not pending")

    def exists_fn(candidate: str) -> bool:
        return _name_exists_globally(db, candidate, statuses=("published",), exclude_id=skill.id) or (
            PUBLISHED_DIR / candidate
        ).exists()

    if exists_fn(skill.name):
        new_name = _unique_name(skill.name, exists_fn)
        old_dir = _skill_dir(skill)
        skill.name = new_name
        new_dir = _skill_dir(skill)
        _move_dir(old_dir, new_dir)

    old_dir = _skill_dir(skill)
    skill.status = "published"
    skill.scope = "global" if skill.is_public else "user"
    skill.group_id = None
    skill.pending_comment = None
    skill.published_at = _now()
    skill.published_by = _.id
    skill.updated_at = _now()
    new_dir = _skill_dir(skill)
    _move_dir(old_dir, new_dir)
    _log(db, skill.id, _.id, "approve")
    db.commit()
    db.refresh(skill)
    return {"item": _serialize_skill(skill)}


@router.post("/publish_requests/{skill_id}/reject")
def reject_publish(
    skill_id: str,
    payload: RejectSkillRequest,
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if skill.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Skill not pending")
    old_dir = _skill_dir(skill)
    skill.status = "rejected"
    if skill.group_id:
        skill.scope = "group"
    elif skill.is_public:
        skill.scope = "global"
    else:
        skill.scope = "user"
    skill.rejected_at = _now()
    skill.rejected_by = admin_user.id
    skill.rejected_reason = payload.comment
    skill.pending_comment = None
    skill.updated_at = _now()
    new_dir = _skill_dir(skill)
    _move_dir(old_dir, new_dir)
    _log(db, skill.id, admin_user.id, "reject", {"comment": payload.comment})
    db.commit()
    db.refresh(skill)
    return {"item": _serialize_skill(skill)}


@router.post("/{skill_id}/save_to_mine")
def save_agent_skill(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if skill.owner_user_id != current_user.id or skill.source_type != "agent":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    def exists_fn(candidate: str) -> bool:
        return (
            db.scalar(
                select(Skill.id).where(
                    Skill.owner_user_id == current_user.id,
                    Skill.name == candidate,
                    Skill.id != skill.id,
                )
            )
            is not None
            or _userskill_dir_exists(current_user.id, candidate)
        )

    if exists_fn(skill.name):
        new_name = _unique_name(skill.name, exists_fn)
        old_dir = _skill_dir(skill)
        skill.name = new_name
        new_dir = _skill_dir(skill)
        _move_dir(old_dir, new_dir)

    old_dir = _skill_dir(skill)
    skill.source_type = "user"
    skill.status = "draft"
    skill.scope = "user"
    skill.group_id = None
    skill.conversation_id = None
    skill.updated_at = _now()
    new_dir = _skill_dir(skill)
    _move_dir(old_dir, new_dir)
    _log(db, skill.id, current_user.id, "save")
    db.commit()
    db.refresh(skill)
    return {"item": _serialize_skill(skill)}


@router.post("/{skill_id}/copy")
def copy_public_skill(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    builtin_meta: dict | None = None
    builtin_dir: Path | None = None
    if skill_id.startswith("builtin:"):
        resolved = _resolve_builtin_skill(skill_id.split(":", 1)[1])
        if not resolved:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
        builtin_meta, builtin_dir = resolved
    else:
        if not skill:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
        if skill.status != "published" or not skill.is_public_edit:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")

    base_name = builtin_meta["name"] if builtin_meta else skill.name

    def exists_fn(candidate: str) -> bool:
        return _name_exists_for_owner(db, current_user.id, candidate) or _userskill_dir_exists(current_user.id, candidate)

    new_name = _unique_name(base_name, exists_fn)
    new_skill = Skill(
        id=str(uuid4()),
        owner_user_id=current_user.id,
        source_type="user",
        status="draft",
        scope="user",
        group_id=None,
        conversation_id=None,
        name=new_name,
        display_name=builtin_meta["display_name"] if builtin_meta else skill.display_name,
        description=builtin_meta["description"] if builtin_meta else skill.description,
        is_public=False,
        is_public_edit=False,
        cloned_from_skill_id=skill.id if skill else None,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(new_skill)
    _log(
        db,
        new_skill.id,
        current_user.id,
        "copy",
        {"source_skill_id": skill.id if skill else None, "source_builtin": builtin_meta["name"] if builtin_meta else None},
    )
    db.commit()
    try:
        _copy_dir(builtin_dir or _skill_dir(skill), _skill_dir(new_skill))
    except Exception as exc:  # noqa: BLE001
        db.delete(new_skill)
        db.commit()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return {"item": _serialize_skill(new_skill)}


@router.get("/{skill_id}/tree")
def get_skill_tree(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    _require_view_skill(db, current_user, skill)
    root = _skill_dir(skill)
    if not root.exists():
        return {"items": []}
    items: list[dict] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        items.append({"path": rel, "is_dir": path.is_dir()})
    return {"items": items}


@router.get("/{skill_id}/file")
def read_skill_file(
    skill_id: str,
    path: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    _require_view_skill(db, current_user, skill)
    root = _skill_dir(skill)
    file_path = _resolve_path(root, path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    content = file_path.read_text(encoding="utf-8", errors="replace")
    return {"content": content}


@router.put("/{skill_id}/file")
def write_skill_file(
    skill_id: str,
    payload: FileWriteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if not _can_edit_skill(current_user, skill) and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    root = _skill_dir(skill)
    file_path = _resolve_path(root, payload.path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(payload.content or "", encoding="utf-8")
    _log(db, skill.id, current_user.id, "edit_file", {"path": payload.path})
    db.commit()
    return {"message": "ok"}


@router.post("/{skill_id}/dir")
def create_skill_dir(
    skill_id: str,
    payload: DirCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if not _can_edit_skill(current_user, skill) and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    root = _skill_dir(skill)
    dir_path = _resolve_path(root, payload.path)
    dir_path.mkdir(parents=True, exist_ok=True)
    _log(db, skill.id, current_user.id, "create_dir", {"path": payload.path})
    db.commit()
    return {"message": "ok"}


@router.post("/{skill_id}/rename")
def rename_skill_path(
    skill_id: str,
    payload: RenameRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if not _can_edit_skill(current_user, skill) and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    if payload.from_path == "SKILL.md" or payload.to_path == "SKILL.md":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SKILL.md cannot be renamed")
    root = _skill_dir(skill)
    src = _resolve_path(root, payload.from_path)
    dest = _resolve_path(root, payload.to_path)
    if not src.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Path not found")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)
    _log(db, skill.id, current_user.id, "rename", {"from": payload.from_path, "to": payload.to_path})
    db.commit()
    return {"message": "ok"}


@router.delete("/{skill_id}/path")
def delete_skill_path(
    skill_id: str,
    path: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if not _can_edit_skill(current_user, skill) and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    if path == "SKILL.md":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SKILL.md cannot be deleted")
    root = _skill_dir(skill)
    target = _resolve_path(root, path)
    if not target.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Path not found")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    _log(db, skill.id, current_user.id, "delete_path", {"path": path})
    db.commit()
    return {"message": "ok"}


@router.get("/groups/list")
def list_groups(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    groups = db.scalars(select(SkillGroup).order_by(SkillGroup.created_at.desc())).all()
    if not groups:
        return {"items": []}
    group_ids = [g.id for g in groups]
    group_skill_rows = db.execute(
        select(SkillGroupSkill.group_id, Skill.id, Skill.name, Skill.display_name)
        .join(Skill, Skill.id == SkillGroupSkill.skill_id)
        .where(SkillGroupSkill.group_id.in_(group_ids))
        .order_by(Skill.created_at.desc())
    ).all()
    skills_by_group: dict[str, list[dict]] = {gid: [] for gid in group_ids}
    for row in group_skill_rows:
        skills_by_group[row.group_id].append(
            {"id": row.id, "name": row.name, "display_name": row.display_name}
        )
    return {
        "items": [
            {**_serialize_group(g), "skills": skills_by_group.get(g.id, [])}
            for g in groups
        ]
    }


@router.get("/groups/options")
def list_group_options(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    skills = db.scalars(
        select(Skill)
        .where(Skill.status == "published", Skill.scope.in_(("user", "group")))
        .order_by(Skill.created_at.desc())
    ).all()
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return {
        "skills": [_serialize_skill(s) for s in skills],
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name,
                "email": u.email,
                "is_admin": u.is_admin,
            }
            for u in users
        ],
    }


@router.post("/groups")
def create_group(
    payload: CreateGroupRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    existing = db.scalar(select(SkillGroup).where(SkillGroup.name == payload.name))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group name already exists")
    group = SkillGroup(
        id=str(uuid4()),
        name=payload.name,
        description=payload.description,
        created_by=current_user.id,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return {"item": _serialize_group(group)}


@router.patch("/groups/{group_id}")
def update_group(
    group_id: str,
    payload: CreateGroupRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    group = db.get(SkillGroup, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    existing = db.scalar(select(SkillGroup).where(SkillGroup.name == payload.name, SkillGroup.id != group_id))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group name already exists")
    group.name = payload.name
    group.description = payload.description
    group.updated_at = _now()
    db.add(group)
    db.commit()
    db.refresh(group)
    return {"item": _serialize_group(group)}


@router.delete("/groups/{group_id}")
def delete_group(group_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    group = db.get(SkillGroup, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    db.query(SkillGroupSkill).filter(SkillGroupSkill.group_id == group_id).delete()
    db.query(SkillGroupUser).filter(SkillGroupUser.group_id == group_id).delete()
    db.delete(group)
    db.commit()
    return {"message": "deleted"}


@router.get("/groups/{group_id}/skills")
def list_group_skills(group_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    skill_ids = db.scalars(select(SkillGroupSkill.skill_id).where(SkillGroupSkill.group_id == group_id)).all()
    if not skill_ids:
        return {"items": []}
    skills = db.scalars(select(Skill).where(Skill.id.in_(skill_ids))).all()
    return {"items": [_serialize_skill(s) for s in skills]}


@router.post("/groups/{group_id}/skills")
def add_group_skill(
    group_id: str,
    payload: AddSkillToGroupRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    group = db.get(SkillGroup, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    skill = db.get(Skill, payload.skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if skill.status != "published" or skill.is_public:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Skill must be published and non-public")
    exists = db.get(SkillGroupSkill, {"group_id": group_id, "skill_id": payload.skill_id})
    if not exists:
        db.add(SkillGroupSkill(group_id=group_id, skill_id=payload.skill_id))
        skill.scope = "group"
        skill.group_id = group_id
        db.commit()
    return {"message": "ok"}


@router.delete("/groups/{group_id}/skills/{skill_id}")
def remove_group_skill(
    group_id: str,
    skill_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    skill = db.get(Skill, skill_id)
    db.query(SkillGroupSkill).filter(
        SkillGroupSkill.group_id == group_id, SkillGroupSkill.skill_id == skill_id
    ).delete()
    if skill:
        remaining_group_id = db.scalar(select(SkillGroupSkill.group_id).where(SkillGroupSkill.skill_id == skill_id))
        skill.group_id = remaining_group_id
        if remaining_group_id:
            skill.scope = "group"
        elif skill.status == "published" and skill.is_public:
            skill.scope = "global"
        else:
            skill.scope = "user"
    db.commit()
    return {"message": "ok"}


@router.get("/groups/{group_id}/users")
def list_group_users(group_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    user_ids = db.scalars(select(SkillGroupUser.user_id).where(SkillGroupUser.group_id == group_id)).all()
    if not user_ids:
        return {"items": []}
    users = db.scalars(select(User).where(User.id.in_(user_ids))).all()
    return {
        "items": [
            {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name,
                "email": u.email,
                "is_admin": u.is_admin,
            }
            for u in users
        ]
    }


@router.get("/groups/for_user/{user_id}")
def list_groups_for_user(user_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    group_ids = db.scalars(select(SkillGroupUser.group_id).where(SkillGroupUser.user_id == user_id)).all()
    if not group_ids:
        return {"items": []}
    groups = db.scalars(select(SkillGroup).where(SkillGroup.id.in_(group_ids))).all()
    return {"items": [_serialize_group(g) for g in groups]}


@router.get("/groups/users_map")
def list_group_users_map(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(SkillGroupUser.user_id, SkillGroup.id, SkillGroup.name)
        .join(SkillGroup, SkillGroup.id == SkillGroupUser.group_id)
        .order_by(SkillGroup.name.asc())
    ).all()
    mapping: dict[str, list[dict]] = {}
    for row in rows:
        mapping.setdefault(row.user_id, []).append({"id": row.id, "name": row.name})
    items = [{"user_id": user_id, "groups": groups} for user_id, groups in mapping.items()]
    return {"items": items}


@router.post("/groups/{group_id}/users")
def add_group_user(
    group_id: str,
    payload: AddUserToGroupRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    group = db.get(SkillGroup, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    user = db.get(User, payload.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    exists = db.get(SkillGroupUser, {"group_id": group_id, "user_id": payload.user_id})
    if not exists:
        db.add(SkillGroupUser(group_id=group_id, user_id=payload.user_id))
        db.commit()
    return {"message": "ok"}


@router.delete("/groups/{group_id}/users/{user_id}")
def remove_group_user(
    group_id: str,
    user_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    db.query(SkillGroupUser).filter(
        SkillGroupUser.group_id == group_id, SkillGroupUser.user_id == user_id
    ).delete()
    db.commit()
    return {"message": "ok"}
