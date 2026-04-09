from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user, require_admin
from backend.core.database import get_db
from backend.core.models import User, UserProfile
from backend.core.security import hash_password, validate_password_strength, verify_password

router = APIRouter(prefix="/users", tags=["users"])


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    is_admin: bool = False


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    email: EmailStr | None = None
    is_admin: bool | None = None
    is_blocked: bool | None = None


class UpdateMeRequest(BaseModel):
    display_name: str | None = None
    email: EmailStr | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


def _serialize_user(user: User) -> dict:
    profile = user.profile
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "avatar_url": profile.avatar_url if profile else None,
        "is_admin": user.is_admin,
        "is_blocked": user.is_blocked,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def _ensure_profile(db: Session, user: User) -> UserProfile:
    profile = user.profile
    if profile is None:
        profile = UserProfile(user_id=user.id)
        db.add(profile)
    return profile


def _save_avatar_upload(user_id: str, avatar: UploadFile) -> str:
    content_type = (avatar.content_type or "").lower()
    if content_type not in {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Avatar must be an image file")

    suffix_map = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    suffix = suffix_map.get(content_type, ".bin")
    out_dir = Path("uploads/avatars")
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{user_id}_{uuid4().hex}{suffix}"
    out_path = out_dir / filename

    data = avatar.file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Avatar file is empty")
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Avatar file too large (max 5MB)")
    out_path.write_bytes(data)
    return f"/uploads/avatars/{filename}"


def _admin_count(db: Session) -> int:
    admin_ids = db.scalars(select(User.id).where(User.is_admin.is_(True)))
    return len(list(admin_ids))


def _guard_last_admin_change(db: Session, user: User, new_is_admin: bool, new_is_blocked: bool) -> None:
    if user.is_admin and (not new_is_admin or new_is_blocked):
        if _admin_count(db) <= 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot disable or block the last admin")


@router.get("/me")
def get_me_profile(current_user: User = Depends(get_current_user)) -> dict:
    return _serialize_user(current_user)


@router.patch("/me")
def update_me_profile(
    payload: UpdateMeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    updates = payload.model_dump(exclude_none=True)

    if "email" in updates:
        exists = db.scalar(select(User.id).where(User.email == updates["email"], User.id != current_user.id))
        if exists:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists")
        current_user.email = updates["email"]
    if "display_name" in updates:
        current_user.display_name = updates["display_name"]

    db.commit()
    db.refresh(current_user)
    return _serialize_user(current_user)


@router.post("/me/avatar")
def upload_my_avatar(
    avatar: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    avatar_url = _save_avatar_upload(current_user.id, avatar)
    profile = _ensure_profile(db, current_user)
    profile.avatar_url = avatar_url
    db.commit()
    db.refresh(current_user)
    return _serialize_user(current_user)


@router.post("/me/password")
def change_my_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not verify_password(payload.old_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Old password is incorrect")
    try:
        validate_password_strength(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    current_user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"message": "Password updated successfully"}


@router.get("")
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return {"items": [_serialize_user(u) for u in users]}


@router.post("")
def create_user(payload: CreateUserRequest, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    if db.scalar(select(User.id).where(User.username == payload.username)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
    if db.scalar(select(User.id).where(User.email == payload.email)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists")
    try:
        validate_password_strength(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user = User(
        username=payload.username,
        display_name=payload.display_name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _serialize_user(user)


@router.patch("/{user_id}")
def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    updates = payload.model_dump(exclude_none=True)

    if "email" in updates:
        exists = db.scalar(select(User.id).where(User.email == updates["email"], User.id != user.id))
        if exists:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists")
        user.email = updates["email"]
    if "display_name" in updates:
        user.display_name = updates["display_name"]

    new_is_admin = updates.get("is_admin", user.is_admin)
    new_is_blocked = updates.get("is_blocked", user.is_blocked)
    _guard_last_admin_change(db, user, new_is_admin=new_is_admin, new_is_blocked=new_is_blocked)

    if "is_admin" in updates:
        user.is_admin = updates["is_admin"]
    if "is_blocked" in updates:
        user.is_blocked = updates["is_blocked"]

    db.commit()
    db.refresh(user)
    return _serialize_user(user)


@router.post("/{user_id}/avatar")
def upload_user_avatar(
    user_id: str,
    avatar: UploadFile = File(...),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    avatar_url = _save_avatar_upload(user.id, avatar)
    profile = _ensure_profile(db, user)
    profile.avatar_url = avatar_url
    db.commit()
    db.refresh(user)
    return _serialize_user(user)


@router.post("/{user_id}/password/reset")
def admin_reset_user_password(
    user_id: str,
    payload: AdminResetPasswordRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    try:
        validate_password_strength(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"message": "Password reset successfully"}


@router.delete("/{user_id}")
def delete_user(user_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.is_admin and _admin_count(db) <= 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete the last admin")
    db.delete(user)
    db.commit()
    return {"message": "User deleted"}
