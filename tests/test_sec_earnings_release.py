from __future__ import annotations

from app.ingestion.sec_client import SecClient


def _sec_submissions_payload(doc_name: str = "aapl8k.htm") -> dict:
    return {
        "filings": {
            "recent": {
                "form": ["8-K"],
                "filingDate": ["2026-01-29"],
                "acceptanceDateTime": ["2026-01-29T16:05:10-05:00"],
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
        "苹果提交8-K财报摘要：每股收益2.40美元，营收1243亿美元，服务收入创新高，管理层指引毛利率约46.5%。",
    )

    items, check = client.fetch(session)

    assert check.status == "ONLINE"
    assert check.details["sec_earnings_items"] == 1
    assert len(items) == 1
    item = items[0]
    assert item.metadata["event_type_hint"] == "sec_earnings_release"
    assert item.metadata["sec_item_202"] is True
    assert item.metadata["summary_override"].startswith("苹果提交8-K财报摘要")
    assert item.title.startswith("AAPL reports EPS $2.40")
    assert "LLM_SUMMARY_ZH" in item.body


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
