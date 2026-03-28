from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.ingestion.sec_client import SecClient


def _sec_submissions_payload(doc_name: str = "aapl8k.htm") -> dict:
    as_of = datetime.now(timezone.utc) - timedelta(days=1)
    filing_date = as_of.strftime("%Y-%m-%d")
    acceptance = as_of.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return {
        "filings": {
            "recent": {
                "form": ["8-K"],
                "filingDate": [filing_date],
                "acceptanceDateTime": [acceptance],
                "accessionNumber": ["0000320193-26-000010"],
                "primaryDocument": [doc_name],
            }
        }
    }


def test_sec_client_promotes_item_202_to_independent_event(session, settings):
    client = SecClient(settings.model_copy(update={"enable_sec": True}))
    client._ticker_cik_map = lambda: {"AAPL": "0000320193"}  # noqa: SLF001
    client._round_robin_tickers = lambda _session, _tickers, batch_size=10: ["AAPL"]  # noqa: SLF001
    client._request_json_with_retry = lambda _client, _url: (_sec_submissions_payload(), 200, None)  # noqa: SLF001
    client._fetch_html = lambda _client, url: (  # noqa: SLF001
        '<table><tr><td>EX-99.1</td><td><a href="ex991.htm">earnings release</a></td></tr></table>'
        if url.endswith("-index.html")
        else ""
    )
    client._fetch_filing_text = lambda _client, url, max_chars=8000: (  # noqa: SLF001
        "Item 2.02 Results of Operations and Financial Condition. The company issued a quarterly earnings release."
        if url.endswith("aapl8k.htm")
        else "Apple reports quarterly results with EPS of $2.40 on revenue of $124.3 billion and guides gross margin to 46.5%."
    )
    client._summarize_sec_earnings = lambda ticker, filing_text, exhibit_text: (  # noqa: SLF001
        f"{ticker} reports EPS $2.40 on revenue $124.3B; services record, gross margin guide 46.5%",
        "Apple 8-K earnings summary: EPS $2.40, revenue $124.3B, services revenue reached a record, and management guided gross margin to about 46.5%.",
    )

    items, check = client.fetch(session)

    assert check.status == "ONLINE"
    assert check.details["sec_earnings_items"] == 1
    assert len(items) == 1
    item = items[0]
    assert item.metadata["event_type_hint"] == "sec_earnings_release"
    assert item.metadata["sec_item_202"] is True
    assert item.metadata["summary_override"].startswith("Apple 8-K earnings summary")
    assert item.title.startswith("AAPL reports EPS $2.40")
    assert item.metadata["exhibit_99_1_url"].endswith("ex991.htm")
    assert "LLM_SUMMARY_EN" in item.body


def test_sec_client_leaves_non_earnings_8k_as_generic_item(session, settings):
    client = SecClient(settings.model_copy(update={"enable_sec": True}))
    client._ticker_cik_map = lambda: {"AAPL": "0000320193"}  # noqa: SLF001
    client._round_robin_tickers = lambda _session, _tickers, batch_size=10: ["AAPL"]  # noqa: SLF001
    client._request_json_with_retry = lambda _client, _url: (_sec_submissions_payload(doc_name="aaplother8k.htm"), 200, None)  # noqa: SLF001
    client._fetch_html = lambda _client, _url: ""  # noqa: SLF001
    client._fetch_filing_text = lambda _client, _url, max_chars=8000: (  # noqa: SLF001
        "Item 5.02 Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain Officers."
    )

    items, check = client.fetch(session)

    assert check.status == "ONLINE"
    assert check.details["sec_earnings_items"] == 0
    assert len(items) == 1
    item = items[0]
    assert item.title == "AAPL filed 8-K"
    assert "event_type_hint" not in item.metadata


def test_normalize_sec_doc_url_strips_ix_wrapper(settings):
    client = SecClient(settings.model_copy(update={"enable_sec": True}))
    wrapped = "https://www.sec.gov/ix?doc=/Archives/edgar/data/320193/000032019326000005/aapl-20260129.htm"
    assert client._normalize_sec_doc_url(wrapped) == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000005/aapl-20260129.htm"


def test_sec_client_fetch_can_target_single_ticker_and_stop_after_first_earnings(session, settings):
    client = SecClient(settings.model_copy(update={"enable_sec": True}))
    client._ticker_cik_map = lambda: {"AAPL": "0000320193", "MSFT": "0000789019"}  # noqa: SLF001
    client._request_json_with_retry = lambda _client, _url: (_sec_submissions_payload(), 200, None)  # noqa: SLF001
    client._fetch_html = lambda _client, url: (  # noqa: SLF001
        '<table><tr><td>EX-99.1</td><td><a href="ex991.htm">earnings release</a></td></tr></table>'
        if url.endswith("-index.html")
        else ""
    )
    client._fetch_filing_text = lambda _client, _url, max_chars=8000: (  # noqa: SLF001
        "Item 2.02 Results of Operations and Financial Condition. The company issued a quarterly earnings release."
    )
    client._summarize_sec_earnings = lambda ticker, filing_text, exhibit_text: (  # noqa: SLF001
        f"{ticker} reports quarterly results",
        f"{ticker} earnings summary",
    )

    items, check = client.fetch(
        session,
        tickers=["AAPL"],
        max_forms_per_ticker=1,
        stop_after_first_earnings=True,
    )

    assert check.status == "ONLINE"
    assert check.details["selected_tickers"] == 1
    assert check.details["sec_earnings_items"] == 1
    assert len(items) == 1
    assert items[0].metadata["ticker"] == "AAPL"


def test_sec_client_summary_uses_http_gateway(monkeypatch, settings):
    client = SecClient(
        settings.model_copy(
            update={
                "llm_base_url": "https://api.example.com",
                "llm_api_key": "test-key",
                "sec_summary_model": "gemini-3-flash",
            }
        )
    )
    called = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "First-quarter revenue was $143.8 billion and diluted EPS was $2.84."
                        }
                    }
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None):
            called["url"] = url
            called["headers"] = headers
            called["json"] = json
            return _Resp()

    monkeypatch.setattr("app.ingestion.sec_client.httpx.Client", _Client)

    headline, summary = client._summarize_sec_earnings(
        "AAPL",
        "Item 2.02 Results of Operations and Financial Condition.",
        "Apple posted quarterly revenue of $143.8 billion and diluted EPS of $2.84.",
    )

    assert headline == "AAPL SEC earnings release filed under 8-K Item 2.02"
    assert summary == "First-quarter revenue was $143.8 billion and diluted EPS was $2.84."
    assert called["url"] == "https://api.example.com/v1/chat/completions"
    assert called["headers"]["Authorization"] == "Bearer test-key"
    assert called["json"]["model"] == "gemini-3-flash"
