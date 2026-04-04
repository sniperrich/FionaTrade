from __future__ import annotations

import app.agents.base as base_module
from app.agents.base import AgentSignal, BaseAgent


class _DummyAgent(BaseAgent):
    name = "dummy"

    def analyze(self, session, ticker: str, context: dict | None = None) -> AgentSignal:  # pragma: no cover - unused
        return AgentSignal.no_signal(self.name)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


def test_call_llm_retries_and_returns_success(settings, monkeypatch) -> None:
    llm_settings = settings.model_copy(
        update={"llm_base_url": "https://llm.example", "llm_api_key": "token", "llm_model": "demo"}
    )
    agent = _DummyAgent(llm_settings)
    attempts = {"count": 0}

    class _RetryClient:
        def post(self, _url, json=None):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("temporary failure")
            assert json["model"] == "demo"
            return _FakeResponse("ok")

    monkeypatch.setattr(agent, "_get_http_client", lambda: _RetryClient())
    monkeypatch.setattr(base_module.time, "sleep", lambda *_args, **_kwargs: None)

    result = agent._call_llm("system", "user", max_retries=3)

    assert result == "ok"
    assert attempts["count"] == 3


def test_call_llm_custom_timeout_closes_temp_client(settings, monkeypatch) -> None:
    llm_settings = settings.model_copy(
        update={"llm_base_url": "https://llm.example", "llm_api_key": "token", "llm_model": "demo"}
    )
    agent = _DummyAgent(llm_settings)
    state = {"posts": 0, "closed": False}

    class _TimeoutClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def post(self, _url, json=None):
            state["posts"] += 1
            raise RuntimeError("still failing")

        def close(self) -> None:
            state["closed"] = True

    monkeypatch.setattr(base_module.httpx, "Client", _TimeoutClient)
    monkeypatch.setattr(base_module.time, "sleep", lambda *_args, **_kwargs: None)

    result = agent._call_llm("system", "user", timeout_seconds=1.0, max_retries=2)

    assert result is None
    assert state["posts"] == 2
    assert state["closed"] is True
