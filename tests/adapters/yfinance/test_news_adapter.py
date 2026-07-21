from datetime import datetime, timezone

import pytest

from app.stocks.adapters.yfinance.news_adapter import YfinanceNewsProvider
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.company.news.entities import StockNews


class _FakeTicker:
    def __init__(self, news=None, error=None) -> None:
        self._news = news
        self._error = error

    @property
    def news(self):
        if self._error is not None:
            raise self._error
        return self._news


def provider_with(news=None, error=None) -> YfinanceNewsProvider:
    return YfinanceNewsProvider(ticker_factory=lambda symbol: _FakeTicker(news, error))


def _item(
    article_id,
    *,
    title="Headline",
    pub="2026-07-08T20:04:28Z",
    provider="Reuters",
    canonical="https://example.com/a",
    clickthrough=None,
    ctype="STORY",
    summary="A plain-text blurb.",
    thumb="https://img.example.com/a.jpg",
):
    content = {
        "id": article_id,
        "title": title,
        "pubDate": pub,
        "contentType": ctype,
        "provider": {"displayName": provider} if provider else None,
        "canonicalUrl": {"url": canonical} if canonical else None,
        "clickThroughUrl": {"url": clickthrough} if clickthrough else None,
        "summary": summary,
        "thumbnail": {"originalUrl": thumb} if thumb else None,
    }
    return {"id": article_id, "content": content}


def test_maps_fields_and_orders_newest_first():
    # Out of order on purpose — the adapter must return newest first.
    news = provider_with(
        [
            _item("a1", pub="2026-07-01T10:00:00Z", title="Old"),
            _item("a3", pub="2026-07-08T20:04:28Z", title="New"),
            _item("a2", pub="2026-07-05T12:00:00Z", title="Mid"),
        ]
    ).get_news("AAPL")

    assert isinstance(news, StockNews)
    assert [a.id for a in news.articles] == ["a3", "a2", "a1"]
    latest = news.latest
    assert latest.title == "New"
    assert latest.published_at == datetime(2026, 7, 8, 20, 4, 28, tzinfo=timezone.utc)
    assert latest.publisher == "Reuters"
    assert latest.link == "https://example.com/a"
    assert latest.summary == "A plain-text blurb."
    assert latest.thumbnail_url == "https://img.example.com/a.jpg"
    assert latest.content_type == "STORY" and latest.is_video is False


def test_video_content_type_sets_is_video():
    news = provider_with([_item("a1", ctype="VIDEO")]).get_news("AAPL")
    assert news.latest.is_video is True


def test_link_falls_back_to_clickthrough_when_no_canonical():
    news = provider_with(
        [_item("a1", canonical=None, clickthrough="https://example.com/click")]
    ).get_news("AAPL")
    assert news.latest.link == "https://example.com/click"


def test_missing_optional_fields_are_none_not_errors():
    news = provider_with(
        [_item("a1", provider=None, canonical=None, clickthrough=None, thumb=None, summary=None)]
    ).get_news("AAPL")
    a = news.latest
    assert a.publisher is None and a.link is None
    assert a.summary is None and a.thumbnail_url is None
    assert a.title == "Headline"  # the required fields still map


def test_rows_without_id_title_or_publish_time_are_dropped():
    news = provider_with(
        [
            _item("a1"),  # good
            _item("a2", title=""),  # no title
            _item("a3", pub=""),  # no publish time
            _item("a4", pub="not-a-date"),  # unparseable publish time
            {"id": "a5"},  # no content at all
        ]
    ).get_news("AAPL")
    assert [a.id for a in news.articles] == ["a1"]


def test_duplicate_ids_keep_the_first_row():
    news = provider_with(
        [_item("a1", title="first"), _item("a1", title="second")]
    ).get_news("AAPL")
    assert len(news.articles) == 1
    assert news.latest.title == "first"


def test_empty_list_is_no_news_not_an_error():
    news = provider_with([]).get_news("ZZZZ")
    assert news.is_empty
    assert news.latest is None


def test_missing_list_is_no_news_not_an_error():
    news = provider_with(None).get_news("ZZZZ")
    assert news.is_empty


def test_vendor_failure_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider_with(error=RuntimeError("rate limited")).get_news("AAPL")


def test_ticker_construction_failure_raises_unavailable():
    def _boom(symbol):
        raise RuntimeError("no network")

    provider = YfinanceNewsProvider(ticker_factory=_boom)
    with pytest.raises(StockDataUnavailable):
        provider.get_news("AAPL")
