from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
        db.commit()
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
