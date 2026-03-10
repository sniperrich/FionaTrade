from __future__ import annotations

from app.analysis.service import AnalysisService
from app.db.models import Event
from app.core.utils import utc_now


def test_analysis_fallback_used_when_llm_not_configured(settings):
    event = Event(
        event_type="financial_fraud",
        entities=["AAPL"],
        tickers=["AAPL"],
        severity=90,
        event_time=utc_now(),
        confidence=88,
        validation_status="VALID",
        summary="Possible accounting fraud",
    )

    signal = AnalysisService(settings).event_to_signal(event)

    assert signal is not None
    assert signal.fallback_used is True
    assert signal.action == "SHORT"


def test_analysis_llm_mode_without_api_key(settings):
    settings.llm_base_url = "https://api.duojie.games"
    settings.llm_model = "claude-sonnet-4-5"
    settings.llm_api_key = ""

    event = Event(
        event_type="merger_acquisition",
        entities=["AAPL"],
        tickers=["AAPL"],
        severity=70,
        event_time=utc_now(),
        confidence=86,
        validation_status="VALID",
        summary="M&A activity",
    )

    svc = AnalysisService(settings)
    svc._llm_extract = lambda _event, *_args, **_kwargs: {  # noqa: SLF001
        "action": "BUY",
        "ticker": "AAPL",
        "horizon_min": 60,
        "horizon_profile": "MID",
        "reason": "llm_gateway_ok",
    }

    signal = svc.event_to_signal(event)
    assert signal is not None
    assert signal.fallback_used is False
    assert signal.action == "BUY"
    assert signal.horizon_profile == "MID"
