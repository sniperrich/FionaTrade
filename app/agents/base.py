from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_app_logger

logger = get_app_logger()


class AgentSignal:
    """Structured output from a single agent."""

    __slots__ = ("agent_name", "signal", "confidence", "reasoning", "metadata", "error")

    def __init__(
        self,
        agent_name: str,
        signal: str,
        confidence: int,
        reasoning: str,
        metadata: dict | None = None,
        error: str | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.signal = signal          # BUY / SHORT / HOLD / NO_SIGNAL
        self.confidence = confidence  # 0-100
        self.reasoning = reasoning
        self.metadata = metadata or {}
        self.error = error

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "signal": self.signal,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "metadata": self.metadata,
            "error": self.error,
        }

    @classmethod
    def no_signal(cls, agent_name: str, reason: str = "") -> "AgentSignal":
        return cls(agent_name=agent_name, signal="NO_SIGNAL", confidence=0, reasoning=reason)

    @classmethod
    def error_signal(cls, agent_name: str, error: str) -> "AgentSignal":
        return cls(agent_name=agent_name, signal="NO_SIGNAL", confidence=0, reasoning="", error=error)


class BaseAgent(ABC):
    """Base class for all FionaTrade agents."""

    name: str = "base_agent"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http_client: httpx.Client | None = None

    # ── LLM helpers ───────────────────────────────────────────────────────────

    def _get_http_client(self) -> httpx.Client:
        if self._http_client is None:
            # Use transport-level retries for SSL/connection errors
            transport = httpx.HTTPTransport(retries=3)
            self._http_client = httpx.Client(
                timeout=self.settings.llm_timeout_seconds,
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                transport=transport,
            )
        return self._http_client

    def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        response_format: str = "text",
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> str | None:
        """Call the configured LLM endpoint. Returns the response text or None on failure."""
        if not self.settings.llm_base_url:
            return None

        model_name = model or self.settings.llm_model
        retries = max(1, int(max_retries if max_retries is not None else self.settings.llm_max_retries))

        # Some proxy-routed models (e.g. Kiro) reject system prompts that
        # assign an identity. Merge system prompt into user prompt instead.
        if self.settings.llm_merge_system_prompt:
            messages = [
                {"role": "user", "content": f"{system_prompt}\n\n---\n\n{user_prompt}"},
            ]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 1500,
        }
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        url = self.settings.llm_base_url.rstrip("/") + "/v1/chat/completions"
        delay = self.settings.llm_retry_backoff_seconds

        # Use the shared client unless this call needs a custom timeout.
        use_custom_timeout = timeout_seconds is not None
        custom_client: httpx.Client | None = None
        client = self._get_http_client()
        if use_custom_timeout:
            transport = httpx.HTTPTransport(retries=3)
            custom_client = httpx.Client(
                timeout=max(1.0, float(timeout_seconds)),
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                transport=transport,
            )
            client = custom_client

        try:
            for attempt in range(retries):
                try:
                    resp = client.post(url, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
                except Exception as exc:
                    logger.warning(
                        "[%s] LLM call failed (attempt %d/%d): %s",
                        self.name,
                        attempt + 1,
                        retries,
                        exc,
                    )
                    if attempt < retries - 1:
                        time.sleep(min(delay, self.settings.llm_retry_max_delay_seconds))
                        delay *= self.settings.llm_retry_backoff_multiplier
        finally:
            if custom_client is not None:
                custom_client.close()

        return None

    def _parse_json_response(self, text: str | None) -> dict | None:
        """Parse JSON from LLM response, stripping markdown code fences if present."""
        if not text:
            return None
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.split("\n")
            stripped = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            logger.debug("[%s] Failed to parse JSON response: %s", self.name, stripped[:200])
            return None

    # ── Abstract interface ────────────────────────────────────────────────────

    def _get_performance_context(self, context: dict | None) -> str:
        """Extract this agent's performance feedback from context (if available)."""
        if not context:
            return ""
        agent_perf = context.get("agent_performance", {})
        return agent_perf.get(self.name, "")

    def _get_market_time_context(self, context: dict | None) -> str:
        """Return market time string to prepend to system prompts.

        In live mode this tells agents the current US market session so they
        can calibrate urgency (e.g., 'last 30 min of trading' vs 'just opened').
        Returns empty string in backtest mode (as_of set instead).
        """
        if not context:
            return ""
        if context.get("as_of"):
            return ""  # backtest mode — do not inject live time
        market_time = context.get("market_time", "")
        if not market_time:
            return ""
        return f"[{market_time}]\n"

    @abstractmethod
    def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
        """Run analysis and return a structured signal.

        Args:
            session: SQLAlchemy session (read-only; agents must not commit)
            ticker: Uppercase ticker symbol (e.g. "AAPL")
            context: Optional shared context dict from the agent graph state
        """
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
