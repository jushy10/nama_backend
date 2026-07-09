"""Tests for the recommendations use cases: GetStockRecommendations + SyncRecommendations.

Offline: hand-written fakes for the provider and repository ports, so this exercises only
the orchestration — symbol normalization and pass-through on the read side; which targets
are refreshed, failure/empty handling, and the per-run limit on the sync side — plus the
entity rules the slice's responses lean on (score, consensus bands, direction), independent
of yfinance or the DB.
"""

from datetime import date

import pytest

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.recommendations.entities import (
    AnalystPriceTargets,
    AnalystRatingChanges,
    AnalystRecommendations,
    RatingChange,
    RecommendationTrend,
)
from app.stocks.recommendations.ports import (
    RatingChangeProvider,
    RecommendationProvider,
)
from app.stocks.recommendations.repository import (
    RatingChangesRepository,
    RecommendationsRepository,
    RefreshTarget,
)
from app.stocks.recommendations.use_cases import (
    GetStockRatingChanges,
    GetStockRecommendations,
    RecommendationsSyncReport,
    SyncRecommendations,
)


def _a_trend(period, *, strong_buy=0, buy=0, hold=0, sell=0, strong_sell=0):
    return RecommendationTrend(
        period=period,
        strong_buy=strong_buy,
        buy=buy,
        hold=hold,
        sell=sell,
        strong_sell=strong_sell,
    )


def _a_run(symbol: str) -> AnalystRecommendations:
    return AnalystRecommendations(
        symbol, (_a_trend(date(2026, 6, 1), strong_buy=13, buy=24, hold=7),)
    )


# ───────────────────────────── entity rules ─────────────────────────────


def test_trend_total_score_and_consensus():
    t = _a_trend(date(2026, 6, 1), strong_buy=13, buy=24, hold=7)
    assert t.total == 44
    # weighted = 13*1 + 24*2 + 7*3 = 82; 82 / 44 = 1.86
    assert t.score == 1.86
    assert t.consensus == "Buy"  # 1.5 < 1.86 <= 2.5


def test_trend_empty_has_no_score():
    t = _a_trend(date(2026, 6, 1))
    assert t.total == 0
    assert t.score is None
    assert t.consensus is None


@pytest.mark.parametrize(
    "trend, label",
    [
        (_a_trend(date(2026, 6, 1), strong_buy=10), "Strong Buy"),  # mean 1.0
        (_a_trend(date(2026, 6, 1), hold=10), "Hold"),  # mean 3.0
        (_a_trend(date(2026, 6, 1), strong_sell=10), "Strong Sell"),  # mean 5.0
    ],
)
def test_consensus_bands(trend, label):
    assert trend.consensus == label


def test_direction_upgraded_when_more_bullish():
    newer = _a_trend(date(2026, 6, 1), strong_buy=20, buy=10, hold=2)  # mean 1.44
    older = _a_trend(date(2026, 5, 1), strong_buy=5, buy=10, hold=15, sell=2)  # 2.44
    recs = AnalystRecommendations("AAPL", (newer, older))
    assert recs.latest is newer
    assert recs.direction == "upgraded"


def test_direction_none_with_one_snapshot():
    recs = AnalystRecommendations("AAPL", (_a_trend(date(2026, 6, 1), buy=1),))
    assert recs.direction is None


def test_empty_run_has_no_latest_or_direction():
    recs = AnalystRecommendations("ZZZZ", ())
    assert recs.is_empty
    assert recs.latest is None
    assert recs.direction is None


# ───────────────────────────── price targets ─────────────────────────────


def test_price_targets_upside_percent():
    targets = AnalystPriceTargets(mean=315.0, high=400.0, low=215.0, median=315.0)
    # (315 - 300) / 300 * 100 = 5.0
    assert targets.upside_percent(300.0) == 5.0
    assert targets.upside_percent(315.0) == 0.0
    assert targets.upside_percent(350.0) == -10.0  # below target → negative upside


def test_price_targets_upside_percent_guards():
    assert AnalystPriceTargets().upside_percent(300.0) is None  # no mean target
    assert AnalystPriceTargets(mean=315.0).upside_percent(None) is None  # no price
    assert AnalystPriceTargets(mean=315.0).upside_percent(0.0) is None  # non-positive price


def test_price_targets_is_empty():
    assert AnalystPriceTargets().is_empty
    assert not AnalystPriceTargets(mean=315.0).is_empty


# ───────────────────────────── rating changes ─────────────────────────────


def test_rating_change_direction_flags():
    up = RatingChange("A", date(2026, 6, 1), action="up")
    down = RatingChange("B", date(2026, 6, 1), action="down")
    maintain = RatingChange("C", date(2026, 6, 1), action="main")
    assert up.is_upgrade and not up.is_downgrade
    assert down.is_downgrade and not down.is_upgrade
    assert not maintain.is_upgrade and not maintain.is_downgrade


def test_rating_changes_latest_and_empty():
    newer = RatingChange("A", date(2026, 6, 2))
    older = RatingChange("B", date(2026, 5, 1))
    run = AnalystRatingChanges("AAPL", (newer, older))
    assert run.latest is newer
    empty = AnalystRatingChanges("ZZZZ", ())
    assert empty.is_empty and empty.latest is None


# ───────────────────────────── GetStockRecommendations ─────────────────────────────


class _FakeReadProvider(RecommendationProvider):
    def __init__(self, recommendations: AnalystRecommendations) -> None:
        self._recommendations = recommendations
        self.calls: list[str] = []

    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        self.calls.append(symbol)
        return self._recommendations


def test_get_normalizes_the_symbol_before_calling_the_provider():
    recs = AnalystRecommendations("AAPL", ())
    provider = _FakeReadProvider(recs)

    out = GetStockRecommendations(provider).execute("  aapl ")

    assert out is recs
    assert provider.calls == ["AAPL"]  # trimmed + upper-cased once, at the edge


def test_get_rejects_a_blank_symbol():
    provider = _FakeReadProvider(AnalystRecommendations("", ()))
    with pytest.raises(ValueError):
        GetStockRecommendations(provider).execute("   ")
    assert provider.calls == []  # rejected before the provider is touched


def test_get_rejects_obviously_invalid_symbols():
    provider = _FakeReadProvider(AnalystRecommendations("", ()))
    for bad in ("123", "TOOLONG", "BR.K"):
        with pytest.raises(ValueError):
            GetStockRecommendations(provider).execute(bad)
    assert provider.calls == []


# ───────────────────────────── GetStockRatingChanges ─────────────────────────────


class _FakeRatingChangeReadProvider(RatingChangeProvider):
    def __init__(self, rating_changes: AnalystRatingChanges) -> None:
        self._rating_changes = rating_changes
        self.calls: list[str] = []

    def get_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        self.calls.append(symbol)
        return self._rating_changes


def test_get_rating_changes_normalizes_the_symbol_before_calling_the_provider():
    changes = AnalystRatingChanges("AAPL", (RatingChange("A Firm", date(2026, 6, 1)),))
    provider = _FakeRatingChangeReadProvider(changes)

    out = GetStockRatingChanges(provider).execute("  aapl ")

    assert out is changes
    assert provider.calls == ["AAPL"]  # trimmed + upper-cased once, at the edge


def test_get_rating_changes_returns_empty_coverage_as_is():
    empty = AnalystRatingChanges("ZZZZ", ())
    provider = _FakeRatingChangeReadProvider(empty)
    out = GetStockRatingChanges(provider).execute("ZZZZ")
    assert out.is_empty  # no coverage is not an error


def test_get_rating_changes_rejects_a_blank_symbol():
    provider = _FakeRatingChangeReadProvider(AnalystRatingChanges("", ()))
    with pytest.raises(ValueError):
        GetStockRatingChanges(provider).execute("   ")
    assert provider.calls == []  # rejected before the provider is touched


# ───────────────────────────── SyncRecommendations ─────────────────────────────


class _FakeRepo(RecommendationsRepository):
    """Serves a fixed target list and records what got upserted."""

    def __init__(self, targets: list[RefreshTarget]) -> None:
        self._targets = list(targets)
        self.upserts: list[tuple[str, str | None]] = []
        self.refresh_limit: int | None = None

    def get(self, symbol: str) -> AnalystRecommendations | None:  # unused here
        return None

    def upsert(self, symbol, name, recommendations) -> None:
        self.upserts.append((symbol, name))

    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        self.refresh_limit = limit
        return self._targets[:limit]


class _FakeSyncProvider(RecommendationProvider):
    """Returns a canned run per symbol, an empty one, or raises."""

    def __init__(self, *, empty=(), errors=None) -> None:
        self._empty = set(empty)
        self._errors = errors or {}
        self.calls: list[str] = []

    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        self.calls.append(symbol)
        if symbol in self._errors:
            raise self._errors[symbol]
        if symbol in self._empty:
            return AnalystRecommendations(symbol, ())
        return _a_run(symbol)


def test_sync_refreshes_every_target_and_reports_counts():
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("MSFT", None)])
    provider = _FakeSyncProvider()

    report = SyncRecommendations(provider, repo).execute(limit=10)

    assert isinstance(report, RecommendationsSyncReport)
    assert (report.refreshed, report.failed, report.limit) == (2, 0, 10)
    assert provider.calls == ["AAPL", "MSFT"]  # stalest-first order
    assert repo.upserts == [("AAPL", "Apple Inc."), ("MSFT", None)]


def test_sync_carries_the_stored_name_through_to_upsert():
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc.")])
    SyncRecommendations(_FakeSyncProvider(), repo).execute()
    assert repo.upserts == [("AAPL", "Apple Inc.")]


def test_sync_counts_failures_and_keeps_going():
    repo = _FakeRepo(
        [RefreshTarget("AAPL", None), RefreshTarget("BAD", None), RefreshTarget("MSFT", None)]
    )
    provider = _FakeSyncProvider(errors={"BAD": StockDataUnavailable("BAD", "yahoo down")})

    report = SyncRecommendations(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (2, 1)
    assert [s for s, _ in repo.upserts] == ["AAPL", "MSFT"]  # BAD skipped, not stored


def test_sync_not_found_is_a_failure_not_a_crash():
    repo = _FakeRepo([RefreshTarget("ZZZZ", None)])
    provider = _FakeSyncProvider(errors={"ZZZZ": StockNotFound("ZZZZ")})

    report = SyncRecommendations(provider, repo).execute()

    assert (report.refreshed, report.failed) == (0, 1)
    assert repo.upserts == []


def test_sync_empty_live_result_is_skipped_not_stored():
    # An empty run has nothing to merge, and upserting it wouldn't advance the stock's
    # refresh stamp — skip it and count a failure so the next run retries.
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("GONE", None)])
    provider = _FakeSyncProvider(empty={"GONE"})

    report = SyncRecommendations(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (1, 1)
    assert repo.upserts == [("AAPL", "Apple Inc.")]  # GONE never upserted


def test_sync_defaults_to_unlimited_when_no_limit_is_given():
    repo = _FakeRepo([])
    SyncRecommendations(_FakeSyncProvider(), repo).execute()
    assert repo.refresh_limit is None  # None => process every anchor stock (seed + refresh)


def test_sync_limit_is_passed_through_and_floored_at_one():
    repo = _FakeRepo([])
    SyncRecommendations(_FakeSyncProvider(), repo).execute(limit=5)
    assert repo.refresh_limit == 5

    SyncRecommendations(_FakeSyncProvider(), repo).execute(limit=0)
    assert repo.refresh_limit == 1  # a non-positive cap is floored to one


# ─────────────────────── SyncRecommendations + rating changes ───────────────────────


class _FakeRatingChangeProvider(RatingChangeProvider):
    """Returns a canned rating-change run per symbol, an empty one, or raises."""

    def __init__(self, *, empty=(), errors=None) -> None:
        self._empty = set(empty)
        self._errors = errors or {}
        self.calls: list[str] = []

    def get_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        self.calls.append(symbol)
        if symbol in self._errors:
            raise self._errors[symbol]
        if symbol in self._empty:
            return AnalystRatingChanges(symbol, ())
        return AnalystRatingChanges(symbol, (RatingChange("A Firm", date(2026, 6, 1)),))


class _FakeRatingChangesRepo(RatingChangesRepository):
    def __init__(self, *, fail_on=()) -> None:
        self.upserts: list[tuple[str, str | None]] = []
        self._fail_on = set(fail_on)

    def get(self, symbol: str):  # unused here
        return None

    def upsert(self, symbol, name, rating_changes) -> None:
        if symbol in self._fail_on:
            raise RuntimeError("db write blew up")
        self.upserts.append((symbol, name))


def test_sync_also_stores_rating_changes_for_refreshed_stocks():
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("MSFT", None)])
    rc_provider = _FakeRatingChangeProvider()
    rc_repo = _FakeRatingChangesRepo()

    report = SyncRecommendations(
        _FakeSyncProvider(),
        repo,
        rating_change_provider=rc_provider,
        rating_change_repository=rc_repo,
    ).execute()

    assert report.refreshed == 2
    assert report.rating_changes_refreshed == 2
    assert rc_provider.calls == ["AAPL", "MSFT"]  # carried the stored name through
    assert rc_repo.upserts == [("AAPL", "Apple Inc."), ("MSFT", None)]


def test_sync_skips_rating_changes_when_recommendations_are_empty():
    # An empty recommendations result short-circuits the stock (counted failed) before the
    # rating-change leg — so a symbol with no trend coverage isn't fetched for events either.
    repo = _FakeRepo([RefreshTarget("GONE", None)])
    rc_provider = _FakeRatingChangeProvider()

    report = SyncRecommendations(
        _FakeSyncProvider(empty={"GONE"}),
        repo,
        rating_change_provider=rc_provider,
        rating_change_repository=_FakeRatingChangesRepo(),
    ).execute()

    assert (report.refreshed, report.rating_changes_refreshed) == (0, 0)
    assert rc_provider.calls == []  # never reached for an uncovered stock


def test_sync_rating_change_failure_never_sinks_the_recommendations_refresh():
    # A rating-change provider error and a rating-change repo write error are both swallowed:
    # the recommendations still refresh, only the rating-change count is affected.
    repo = _FakeRepo([RefreshTarget("AAPL", None), RefreshTarget("MSFT", None)])
    rc_provider = _FakeRatingChangeProvider(
        errors={"AAPL": StockDataUnavailable("AAPL", "yahoo down")}
    )
    rc_repo = _FakeRatingChangesRepo(fail_on={"MSFT"})

    report = SyncRecommendations(
        _FakeSyncProvider(),
        repo,
        rating_change_provider=rc_provider,
        rating_change_repository=rc_repo,
    ).execute()

    assert report.refreshed == 2  # both recommendations refreshed regardless
    assert report.rating_changes_refreshed == 0  # AAPL raised, MSFT's write failed
    assert rc_repo.upserts == []


def test_sync_without_rating_change_ports_leaves_that_count_zero():
    # The rating-change leg is opt-in: with no ports wired, the sweep behaves exactly as before.
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc.")])
    report = SyncRecommendations(_FakeSyncProvider(), repo).execute()
    assert (report.refreshed, report.rating_changes_refreshed) == (1, 0)
