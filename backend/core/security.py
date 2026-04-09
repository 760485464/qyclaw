from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import re

import jwt

from backend.core.config import get_settings


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"{salt.hex()}${hashed.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt_hex, hash_hex = password_hash.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(candidate, expected)
    except Exception:  # noqa: BLE001
        return False


def validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise ValueError("Password too weak: minimum length is 8")
    if not re.search(r"[A-Z]", password):
        raise ValueError("Password too weak: must include an uppercase letter")
    if not re.search(r"[a-z]", password):
        raise ValueError("Password too weak: must include a lowercase letter")
    if not re.search(r"[0-9]", password):
        raise ValueError("Password too weak: must include a number")


def _jwt_secret() -> str:
    settings = get_settings()
    # Use database password as dev secret fallback.
    return f"{settings.database.password}"


def create_access_token(user_id: str, username: str, is_admin: bool, expires_minutes: int = 60 * 12) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "is_admin": is_admin,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])


def create_password_reset_token(reset_id: str, user_id: str, expires_minutes: int = 10) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "type": "password_reset",
        "rid": reset_id,
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def decode_password_reset_token(token: str) -> dict:
    payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    if payload.get("type") != "password_reset":
        raise jwt.InvalidTokenError("Invalid reset token type")
    return payload
