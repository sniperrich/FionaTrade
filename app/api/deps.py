from __future__ import annotations

from collections.abc import Generator

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.database import SessionLocal


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_app_settings() -> Settings:
    return get_settings()
