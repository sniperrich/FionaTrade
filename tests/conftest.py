from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.database import Base


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        enable_sec=False,
        enable_rss=False,
        enable_finnhub=False,
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        enable_scheduler=False,
    )


@pytest.fixture()
def session():
    # StaticPool ensures all threads share the same in-memory connection,
    # which is required for AgentGraph's ThreadPoolExecutor to see test data.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
        db.commit()
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def session_factory():
    """Yields (session, session_factory) for tests needing per-thread sessions (AgentGraph)."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
    Base.metadata.create_all(bind=engine)
    db = factory()
    try:
        yield db, factory
        db.commit()
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
