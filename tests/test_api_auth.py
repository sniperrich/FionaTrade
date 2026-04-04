from __future__ import annotations

from fastapi import HTTPException
from starlette.requests import Request

from app.api.deps import require_api_access
from app.core.config import Settings


def _request_for_host(host: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/health",
        "headers": [],
        "client": (host, 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope)


def test_require_api_access_allows_loopback_without_key() -> None:
    settings = Settings(_env_file=None, control_api_key="", control_api_localhost_bypass=True)
    require_api_access(_request_for_host("127.0.0.1"), settings=settings)


def test_require_api_access_blocks_remote_when_key_not_configured() -> None:
    settings = Settings(_env_file=None, control_api_key="", control_api_localhost_bypass=False)

    try:
        require_api_access(_request_for_host("203.0.113.7"), settings=settings)
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "CONTROL_API_KEY" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_require_api_access_rejects_invalid_remote_key() -> None:
    settings = Settings(_env_file=None, control_api_key="secret", control_api_localhost_bypass=False)

    try:
        require_api_access(
            _request_for_host("203.0.113.7"),
            settings=settings,
            x_fiona_admin_key="wrong",
        )
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("expected HTTPException")


def test_require_api_access_accepts_valid_admin_header() -> None:
    settings = Settings(_env_file=None, control_api_key="secret", control_api_localhost_bypass=False)
    require_api_access(
        _request_for_host("203.0.113.7"),
        settings=settings,
        x_fiona_admin_key="secret",
    )


def test_require_api_access_accepts_valid_bearer_token() -> None:
    settings = Settings(_env_file=None, control_api_key="secret", control_api_localhost_bypass=False)
    require_api_access(
        _request_for_host("203.0.113.7"),
        settings=settings,
        x_fiona_admin_key=None,
        authorization="Bearer secret",
    )
