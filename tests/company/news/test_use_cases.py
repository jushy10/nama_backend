from datetime import datetime, timezone

import pytest

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.company.news.entities import NewsArticle, StockNews
from app.stocks.company.news.ports import NewsProvider
from app.stocks.company.news.repository import NewsRepository, RefreshTarget
from app.stocks.company.news.use_cases import (
    GetStockNews,
    NewsSyncReport,
    SyncStockNews,
)

_PUB = datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)


def _an_article(article_id="a1", *, published=_PUB, title="Headline", **kw) -> NewsArticle:
    return NewsArticle(id=article_id, title=title, published_at=published, **kw)


def _a_run(symbol: str) -> StockNews:
    return StockNews(symbol, (_an_article(),))


def test_is_video_reads_the_content_type():
    assert _an_article(content_type="VIDEO").is_video is True
    assert _an_article(content_type="video").is_video is True  # case-insensitive
    assert _an_article(content_type="STORY").is_video is False
    assert _an_article(content_type=None).is_video is False


def test_latest_is_the_front_of_the_run():
    newer = _an_article("a2", published=datetime(2026, 7, 8, tzinfo=timezone.utc))
    older = _an_article("a1", published=datetime(2026, 7, 1, tzinfo=timezone.utc))
    news = StockNews("AAPL", (newer, older))
    assert news.latest is newer
    assert not news.is_empty


def test_empty_run_has_no_latest():
    news = StockNews("ZZZZ", ())
    assert news.is_empty
    assert news.latest is None


class _FakeReadProvider(NewsProvider):
    def __init__(self, news: StockNews) -> None:
        self._news = news
        self.calls: list[str] = []

    def get_news(self, symbol: str) -> StockNews:
        self.calls.append(symbol)
        return self._news


def test_get_normalizes_the_symbol_before_calling_the_provider():
    news = StockNews("AAPL", ())
    provider = _FakeReadProvider(news)

    out = GetStockNews(provider).execute("  aapl ")

    assert out is news
    assert provider.calls == ["AAPL"]  # trimmed + upper-cased once, at the edge


def test_get_rejects_a_blank_symbol():
    provider = _FakeReadProvider(StockNews("", ()))
    with pytest.raises(ValueError):
        GetStockNews(provider).execute("   ")
    assert provider.calls == []  # rejected before the provider is touched


def test_get_rejects_obviously_invalid_symbols():
    provider = _FakeReadProvider(StockNews("", ()))
    for bad in ("123", "TOOLONG", "BR.K"):
        with pytest.raises(ValueError):
            GetStockNews(provider).execute(bad)
    assert provider.calls == []


class _FakeRepo(NewsRepository):
    def __init__(self, targets: list[RefreshTarget]) -> None:
        self._targets = list(targets)
        self.upserts: list[tuple[str, str | None]] = []
        self.refresh_limit: int | None = None

    def get(self, symbol: str) -> StockNews | None:  # unused here
        return None

    def upsert(self, symbol, name, news) -> None:
        self.upserts.append((symbol, name))

    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        self.refresh_limit = limit
        return self._targets[:limit]


class _FakeSyncProvider(NewsProvider):
    def __init__(self, *, empty=(), errors=None) -> None:
        self._empty = set(empty)
        self._errors = errors or {}
        self.calls: list[str] = []

    def get_news(self, symbol: str) -> StockNews:
        self.calls.append(symbol)
        if symbol in self._errors:
            raise self._errors[symbol]
        if symbol in self._empty:
            return StockNews(symbol, ())
        return _a_run(symbol)


def test_sync_refreshes_every_target_and_reports_counts():
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("MSFT", None)])
    provider = _FakeSyncProvider()

    report = SyncStockNews(provider, repo).execute(limit=10)

    assert isinstance(report, NewsSyncReport)
    assert (report.refreshed, report.failed, report.limit) == (2, 0, 10)
    assert provider.calls == ["AAPL", "MSFT"]  # stalest-first order
    assert repo.upserts == [("AAPL", "Apple Inc."), ("MSFT", None)]


def test_sync_carries_the_stored_name_through_to_upsert():
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc.")])
    SyncStockNews(_FakeSyncProvider(), repo).execute()
    assert repo.upserts == [("AAPL", "Apple Inc.")]


def test_sync_counts_failures_and_keeps_going():
    repo = _FakeRepo(
        [RefreshTarget("AAPL", None), RefreshTarget("BAD", None), RefreshTarget("MSFT", None)]
    )
    provider = _FakeSyncProvider(errors={"BAD": StockDataUnavailable("BAD", "yahoo down")})

    report = SyncStockNews(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (2, 1)
    assert [s for s, _ in repo.upserts] == ["AAPL", "MSFT"]  # BAD skipped, not stored


def test_sync_not_found_is_a_failure_not_a_crash():
    repo = _FakeRepo([RefreshTarget("ZZZZ", None)])
    provider = _FakeSyncProvider(errors={"ZZZZ": StockNotFound("ZZZZ")})

    report = SyncStockNews(provider, repo).execute()

    assert (report.refreshed, report.failed) == (0, 1)
    assert repo.upserts == []


def test_sync_empty_live_result_is_skipped_not_stored():
    # An empty run has nothing to merge, and upserting it wouldn't advance the stock's
    # refresh stamp — skip it and count a failure so the next run retries.
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("GONE", None)])
    provider = _FakeSyncProvider(empty={"GONE"})

    report = SyncStockNews(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (1, 1)
    assert repo.upserts == [("AAPL", "Apple Inc.")]  # GONE never upserted


def test_sync_defaults_to_unlimited_when_no_limit_is_given():
    repo = _FakeRepo([])
    SyncStockNews(_FakeSyncProvider(), repo).execute()
    assert repo.refresh_limit is None  # None => process every anchor stock (seed + refresh)


def test_sync_limit_is_passed_through_and_floored_at_one():
    repo = _FakeRepo([])
    SyncStockNews(_FakeSyncProvider(), repo).execute(limit=5)
    assert repo.refresh_limit == 5

    SyncStockNews(_FakeSyncProvider(), repo).execute(limit=0)
    assert repo.refresh_limit == 1  # a non-positive cap is floored to one
