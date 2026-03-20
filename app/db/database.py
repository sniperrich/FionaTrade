from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.core.config import get_settings

Base = declarative_base()

settings = get_settings()

engine_kwargs = {"future": True}
if settings.database_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {
        "check_same_thread": False,
        "timeout": settings.sqlite_busy_timeout_seconds,
    }
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20
engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


@contextmanager
def db_session() -> Session:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
