from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user
from backend.core.config import get_settings
from backend.core.database import get_db
from backend.core.models import PasswordResetRequest, User
from backend.core.security import (
    create_access_token,
    create_password_reset_token,
    decode_password_reset_token,
    hash_password,
    validate_password_strength,
    verify_password,
)
from backend.services.mailer import MailerError, send_password_reset_email

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class VerifyResetCodeRequest(BaseModel):
    email: EmailStr
    code: str


class ResetPasswordRequest(BaseModel):
    reset_token: str
    new_password: str = Field(min_length=8, max_length=128)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_value(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _constant_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def _user_payload(user: User) -> dict:
    profile = user.profile
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "avatar_url": profile.avatar_url if profile else None,
        "is_admin": user.is_admin,
        "is_blocked": user.is_blocked,
    }


def _ensure_single_bootstrap_admin(db: Session) -> None:
    has_admin = db.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1))
    if has_admin:
        return
    first_user = db.scalar(select(User).order_by(User.created_at.asc(), User.id.asc()).limit(1))
    if first_user and not first_user.is_admin:
        first_user.is_admin = True
        db.commit()


@router.post("/register")
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> dict:
    exists = db.scalar(select(User).where(or_(User.username == payload.username, User.email == payload.email)))
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username or email already exists")

    has_admin = db.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1))
    should_be_admin = has_admin is None

    user = User(
        username=payload.username,
        display_name=payload.display_name,
        email=payload.email,
        password_hash="",
        is_admin=should_be_admin,
    )
    try:
        validate_password_strength(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    user.password_hash = hash_password(payload.password)

    db.add(user)
    db.commit()
    _ensure_single_bootstrap_admin(db)
    db.refresh(user)

    token = create_access_token(user.id, user.username, user.is_admin)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_payload(user),
    }


@router.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict:
    _ensure_single_bootstrap_admin(db)
    user = db.scalar(select(User).where(or_(User.username == payload.username, User.email == payload.username)))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username/email or password")
    if user.is_blocked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is blocked")

    token = create_access_token(user.id, user.username, user.is_admin)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_payload(user),
    }


@router.get("/me")
def me(current_user: User = Depends(get_current_user)) -> dict:
    return _user_payload(current_user)


@router.post("/password/forgot/request")
def request_password_reset(payload: ForgotPasswordRequest, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.email == payload.email))
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account does not exist")
    if user.is_blocked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is blocked")

    code = f"{secrets.randbelow(1_000_000):06d}"
    link_token = secrets.token_urlsafe(32)
    req = PasswordResetRequest(
        user_id=user.id,
        email=user.email,
        code_hash=_hash_value(code),
        link_token_hash=_hash_value(link_token),
        expires_at=_now().replace(microsecond=0) + timedelta(minutes=10),
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    settings = get_settings()
    reset_link = (
        f"{settings.app.frontend_base_url}/#/login"
        f"?tab=forgot&email={quote_plus(user.email)}"
    )
    if settings.smtp.enabled:
        try:
            send_password_reset_email(
                to_email=user.email,
                code=code,
                reset_link=reset_link,
                expires_minutes=10,
            )
        except MailerError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    response = {"message": "Password reset verification sent", "expires_in_seconds": 600}
    if settings.auth.expose_password_reset_debug:
        response["debug"] = {
            "reset_request_id": req.id,
            "code": code,
            "reset_link": reset_link,
        }
    return response


@router.post("/password/forgot/verify")
def verify_password_reset(payload: VerifyResetCodeRequest, db: Session = Depends(get_db)) -> dict:
    code = (payload.code or "").strip()
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Verification code must be 6 digits")

    user = db.scalar(select(User).where(User.email == payload.email))
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account does not exist")
    if user.is_blocked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is blocked")

    active_requests = db.scalars(
        select(PasswordResetRequest)
        .where(
            PasswordResetRequest.user_id == user.id,
            PasswordResetRequest.used_at.is_(None),
            PasswordResetRequest.expires_at > _now(),
        )
        .order_by(PasswordResetRequest.created_at.desc())
    ).all()
    if not active_requests:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Verification code expired")

    provided_hash = _hash_value(code)
    req = None
    for candidate in active_requests:
        if _constant_equals(provided_hash, candidate.code_hash):
            req = candidate
            break
    if req is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification code")

    req.verified_at = _now()
    db.commit()

    reset_token = create_password_reset_token(req.id, user.id, expires_minutes=10)
    return {"message": "Verification passed", "reset_token": reset_token}


@router.post("/password/forgot/reset")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> dict:
    try:
        token_data = decode_password_reset_token(payload.reset_token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired reset token") from exc

    reset_id = token_data.get("rid")
    user_id = token_data.get("sub")
    if not reset_id or not user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid reset token payload")

    req = db.scalar(select(PasswordResetRequest).where(PasswordResetRequest.id == reset_id))
    if not req or req.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reset request not found")
    if req.used_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reset request already used")
    if req.verified_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reset request not verified")
    if req.expires_at <= _now():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Verification code expired")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account does not exist")
    if user.is_blocked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is blocked")

    try:
        validate_password_strength(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user.password_hash = hash_password(payload.new_password)
    req.used_at = _now()
    db.commit()
    return {"message": "Password updated successfully"}
