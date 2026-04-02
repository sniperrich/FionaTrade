from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.core.utils import ensure_utc, ensure_utc_from_timezone, utc_now
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

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, connection_record) -> None:  # type: ignore[no-redef]
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={int(settings.sqlite_busy_timeout_seconds * 1000)}")
        finally:
            cursor.close()
elif settings.database_url.startswith("postgresql") or settings.database_url.startswith("postgres"):
    @event.listens_for(engine, "connect")
    def _configure_postgres(dbapi_connection, connection_record) -> None:  # type: ignore[no-redef]
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET TIME ZONE 'UTC'")
        finally:
            cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
POSTGRES_NAIVE_LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc
POSTGRES_NAIVE_FUTURE_TOLERANCE = timedelta(minutes=5)


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


def is_sqlite_lock_error(exc: Exception) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def db_backend_name() -> str:
    if settings.database_url.startswith("sqlite"):
        return "sqlite"
    if settings.database_url.startswith("postgresql") or settings.database_url.startswith("postgres"):
        return "postgresql"
    return "unknown"


def session_db_backend_name(session: Session | None) -> str:
    if session is None:
        return db_backend_name()
    try:
        bind = session.get_bind()
    except Exception:
        bind = None
    name = getattr(getattr(bind, "dialect", None), "name", None)
    if name:
        return str(name)
    return db_backend_name()


def normalize_db_datetime(dt: datetime | None, *, backend: str | None = None) -> datetime | None:
    if dt is None:
        return None
    resolved_backend = str((backend or db_backend_name()) or "").lower()
    if dt.tzinfo is None and resolved_backend.startswith("postgres"):
        utc_candidate = ensure_utc(dt)
        local_candidate = ensure_utc_from_timezone(dt, POSTGRES_NAIVE_LOCAL_TZ)
        latest_allowed = utc_now() + POSTGRES_NAIVE_FUTURE_TOLERANCE
        candidates = [candidate for candidate in (utc_candidate, local_candidate) if candidate <= latest_allowed]
        if candidates:
            return max(candidates)
        return min(utc_candidate, local_candidate)
    return ensure_utc(dt)


def db_connection_ok() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
