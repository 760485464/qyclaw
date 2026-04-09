from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.models import Skill, SkillGroupSkill, SkillGroupUser, User
from backend.security.resource_policy import PolicyDecision, allow, deny


def can_view_skill(db: Session, user: User, skill: Skill) -> PolicyDecision:
    if user.is_admin:
        return allow()
    if skill.owner_user_id == user.id:
        return allow()
    if skill.scope == "global":
        return allow() if skill.status == "published" else deny("Skill is not published")
    if skill.scope == "group":
        if skill.status != "published":
            return deny("Skill is not published")
        if skill.group_id:
            is_member = (
                db.scalar(
                    select(SkillGroupUser.user_id).where(
                        SkillGroupUser.group_id == skill.group_id,
                        SkillGroupUser.user_id == user.id,
                    )
                )
                is not None
            )
            return allow() if is_member else deny("Skill is restricted to its group members")
        subq = select(SkillGroupSkill.skill_id).join(
            SkillGroupUser, SkillGroupSkill.group_id == SkillGroupUser.group_id
        ).where(SkillGroupUser.user_id == user.id)
        is_member = db.scalar(select(Skill.id).where(Skill.id == skill.id, Skill.id.in_(subq))) is not None
        return allow() if is_member else deny("Skill is restricted to its group members")
    return deny("Skill is private to its owner")


def can_edit_skill(user: User, skill: Skill) -> PolicyDecision:
    if user.is_admin:
        return allow()
    if skill.owner_user_id != user.id:
        return deny("Skill is owned by another user")
    if skill.source_type == "agent":
        return deny("Agent-generated skills are read-only here")
    if skill.scope in {"global", "group"} and skill.status == "published":
        return deny("Published shared skills cannot be edited directly")
    if skill.status not in {"draft", "rejected"}:
        return deny("Only draft or rejected skills can be edited")
    return allow()
