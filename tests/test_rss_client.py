from __future__ import annotations

from types import SimpleNamespace

from app.ingestion.rss_client import RssClient


def test_fetch_ticker_news_uses_timed_feed_loader(settings, monkeypatch):
    settings.enable_rss = True
    settings.enable_ticker_rss = True
    client = RssClient(settings)

    monkeypatch.setattr("app.ingestion.rss_client.feedparser.parse", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("direct parse should not be used")))

    def fake_load_feed(url: str):
        assert "finance.yahoo.com/rss/headline?s=AAPL" in url
        return SimpleNamespace(
            entries=[
                {
                    "title": "Apple headline",
                    "link": "https://example.com/aapl",
                    "summary": "test body",
                    "published": "2026-04-04T09:00:00Z",
                }
            ],
            bozo=False,
        )

    monkeypatch.setattr(client, "_load_feed", fake_load_feed)
    items, checks = client.fetch_ticker_news(["AAPL"])

    assert len(items) == 1
    assert items[0].metadata["ticker"] == "AAPL"
    assert checks
