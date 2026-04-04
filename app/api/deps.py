from __future__ import annotations

from collections.abc import Generator
from ipaddress import ip_address
from secrets import compare_digest

from fastapi import Depends, Header, HTTPException, Request
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


def _is_loopback_host(host: str | None) -> bool:
    text = str(host or "").strip()
    if not text:
        return False
    if text in {"localhost", "testclient"}:
        return True
    try:
        return ip_address(text).is_loopback
    except ValueError:
        return False


def require_api_access(
    request: Request,
    settings: Settings = Depends(get_app_settings),
    x_fiona_admin_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    host = request.client.host if request.client else None
    if settings.control_api_localhost_bypass and _is_loopback_host(host):
        return

    expected = str(settings.control_api_key or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="CONTROL_API_KEY is not configured; remote API access is disabled",
        )

    candidate = str(x_fiona_admin_key or "").strip()
    if not candidate and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            candidate = token.strip()

    if not candidate or not compare_digest(candidate, expected):
        raise HTTPException(status_code=401, detail="invalid or missing control API key")
