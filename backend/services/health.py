from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from backend.core.config import get_settings


def check_postgres() -> dict[str, Any]:
    settings = get_settings()
    engine = create_engine(settings.database.sqlalchemy_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}
    except SQLAlchemyError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        engine.dispose()

