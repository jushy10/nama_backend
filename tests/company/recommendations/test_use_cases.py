from datetime import date, datetime, timezone

import pytest

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.company.recommendations.entities import (
    AnalystPriceTargets,
    AnalystRatingChanges,
    AnalystRecommendations,
    FIRM_CREDIBILITY,
    FirmRating,
    RatingChange,
    RecommendationTrend,
)
from app.stocks.company.recommendations.ports import (
    RatingChangeProvider,
    RecommendationProvider,
)
from app.stocks.company.recommendations.repository import (
    RatingChangesRepository,
    RecommendationsRepository,
    RefreshTarget,
)
from app.stocks.company.recommendations.use_cases import (
    GetStockAnalystInfo,
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


class _FakeRecommendationReadProvider(RecommendationProvider):
    def __init__(self, recommendations=None, *, error=None) -> None:
        self._recommendations = recommendations
        self._error = error
        self.calls: list[str] = []

    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._recommendations


class _FakeRatingChangeReadProvider(RatingChangeProvider):
    def __init__(self, rating_changes=None, *, error=None) -> None:
        self._rating_changes = rating_changes
        self._error = error
        self.calls: list[str] = []

    def get_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._rating_changes


def test_analyst_info_normalizes_the_symbol_and_composes_both_legs():
    recs = _a_run("AAPL")
    changes = AnalystRatingChanges("AAPL", (RatingChange("A Firm", date(2026, 6, 1)),))
    recs_provider = _FakeRecommendationReadProvider(recs)
    rc_provider = _FakeRatingChangeReadProvider(changes)

    info = GetStockAnalystInfo(recs_provider, rc_provider).execute("  aapl ")

    assert info.symbol == "AAPL"
    assert info.recommendations is recs
    assert info.rating_changes is changes
    # both ports saw the trimmed + upper-cased symbol, once, at the edge
    assert recs_provider.calls == ["AAPL"]
    assert rc_provider.calls == ["AAPL"]


def test_analyst_info_returns_empty_coverage_as_is():
    # No coverage on either leg is a 200-shaped empty result, not an error.
    info = GetStockAnalystInfo(
        _FakeRecommendationReadProvider(AnalystRecommendations("ZZZZ", ())),
        _FakeRatingChangeReadProvider(AnalystRatingChanges("ZZZZ", ())),
    ).execute("ZZZZ")
    assert info.recommendations.is_empty
    assert info.rating_changes.is_empty


def test_analyst_info_rating_changes_failure_is_swallowed():
    # The rating-change leg is best-effort enrichment: a provider failure degrades it to an
    # empty run while the primary trends are still returned.
    recs = _a_run("AAPL")
    for error in (StockNotFound("AAPL"), StockDataUnavailable("AAPL", "yahoo down")):
        info = GetStockAnalystInfo(
            _FakeRecommendationReadProvider(recs),
            _FakeRatingChangeReadProvider(error=error),
        ).execute("AAPL")
        assert info.recommendations is recs  # primary trends survive
        assert info.rating_changes == AnalystRatingChanges("AAPL")  # empty, not an error


def test_analyst_info_recommendations_failure_propagates():
    # The trends are primary — their failure is not swallowed (the endpoint maps it to 404/502).
    for error in (StockNotFound("AAPL"), StockDataUnavailable("AAPL", "yahoo down")):
        use_case = GetStockAnalystInfo(
            _FakeRecommendationReadProvider(error=error),
            _FakeRatingChangeReadProvider(AnalystRatingChanges("AAPL", ())),
        )
        with pytest.raises(type(error)):
            use_case.execute("AAPL")


def test_analyst_info_rejects_invalid_symbols_before_touching_the_providers():
    recs_provider = _FakeRecommendationReadProvider(AnalystRecommendations("", ()))
    rc_provider = _FakeRatingChangeReadProvider(AnalystRatingChanges("", ()))
    use_case = GetStockAnalystInfo(recs_provider, rc_provider)
    for bad in ("   ", "123", "TOOLONG", "BR.K"):
        with pytest.raises(ValueError):
            use_case.execute(bad)
    assert recs_provider.calls == [] and rc_provider.calls == []


def _change(firm, published_at, *, to_grade="Buy", action="main", target=None):
    return RatingChange(
        firm, published_at, action=action, to_grade=to_grade, target_current=target
    )


def test_top_credible_firms_ranks_by_credibility_and_keeps_latest_per_firm():
    changes = AnalystRatingChanges(
        "NVDA",
        (
            # newest-first, the order the store serves them in
            _change("Evercore ISI Group", date(2026, 5, 21), to_grade="Outperform", target=413),
            _change("RBC Capital", date(2026, 5, 21), to_grade="Outperform", target=270),
            _change("Morgan Stanley", date(2026, 5, 21), to_grade="Overweight", target=288),
            _change("Rosenblatt", date(2026, 5, 21), to_grade="Buy", target=325),  # unranked
            _change("RBC Capital", date(2026, 3, 17), to_grade="Outperform", target=250),  # older
        ),
    )
    top = changes.top_credible_firms(5)
    # RBC (rank 1) before Evercore (2) before Morgan Stanley (5); Rosenblatt excluded (unranked).
    assert [f.firm for f in top] == ["RBC Capital", "Evercore ISI Group", "Morgan Stanley"]
    assert isinstance(top[0], FirmRating)
    assert top[0].rank == 1
    assert top[0].target == 270  # the newer RBC row won the dedup, not the older $250 one
    assert top[0].rating == "Outperform"


def test_top_credible_firms_resolves_aliases_and_excludes_lookalikes():
    changes = AnalystRatingChanges(
        "X",
        (
            _change("B of A Securities", date(2026, 5, 21), target=350),  # alias -> Bank of America
            _change("Keybanc", date(2026, 5, 21), target=310),  # NOT KBW — must be excluded
            _change("Cowen", date(2026, 5, 20), target=300),  # alias -> TD Cowen
        ),
    )
    firms = [f.firm for f in changes.top_credible_firms(5)]
    assert "B of A Securities" in firms  # matched via alias
    assert "Cowen" in firms  # matched via alias
    assert "Keybanc" not in firms  # KeyBanc is not KBW


def test_top_credible_firms_caps_and_may_return_fewer():
    one = AnalystRatingChanges("X", (_change("UBS", date(2026, 5, 1)),))
    assert len(one.top_credible_firms(5)) == 1  # only one credible firm covers it
    assert one.top_credible_firms(0) == ()  # a non-positive cap yields none
    assert AnalystRatingChanges("X", ()).top_credible_firms() == ()  # no events → none


def test_top_credible_firms_caps_at_ten_by_default():
    # A stock covered by more than ten credible firms surfaces exactly the ten most credible.
    covering = FIRM_CREDIBILITY[:12]  # twelve ranked firms, each with a stored action
    changes = AnalystRatingChanges(
        "BIGCAP",
        tuple(
            _change(name, date(2026, 5, 21), target=100 + i)
            for i, name in enumerate(covering)
        ),
    )
    top = changes.top_credible_firms()  # default cap
    assert len(top) == 10
    assert [f.firm for f in top] == list(FIRM_CREDIBILITY[:10])  # best-first, ranks 0–9
    assert [f.rank for f in top] == list(range(10))


def test_top_credible_firms_drops_targets_older_than_a_year():
    as_of = date(2026, 7, 10)
    changes = AnalystRatingChanges(
        "X",
        (
            _change("RBC Capital", date(2026, 3, 1), target=270),  # recent → kept (RBC's newest)
            _change("RBC Capital", date(2024, 1, 1), target=100),  # RBC's stale older row, ignored
            _change("UBS", date(2025, 7, 10), target=280),  # exactly one year old → kept (inclusive)
            _change("Evercore ISI Group", date(2025, 7, 9), target=400),  # a day past a year → dropped
            _change("Truist Securities", date(2024, 6, 1), target=307),  # well over a year → dropped
        ),
    )
    top = changes.top_credible_firms(as_of=as_of)
    assert [f.firm for f in top] == ["RBC Capital", "UBS"]  # Evercore + Truist drop off as stale
    assert top[0].target == 270  # RBC's newest in-window action, not its 2024 one
    # No as_of → no recency filter: all four distinct credible firms return.
    assert len(changes.top_credible_firms()) == 4


def test_top_credible_firms_carry_upside_percent():
    top = AnalystRatingChanges(
        "X", (_change("RBC Capital", date(2026, 5, 1), target=270.0),)
    ).top_credible_firms(1)
    assert top[0].upside_percent(200.0) == 35.0  # (270 - 200) / 200 * 100
    assert top[0].upside_percent(None) is None


def test_analyst_info_populates_top_firms_from_rating_changes():
    changes = AnalystRatingChanges(
        "NVDA",
        (
            _change("RBC Capital", date(2026, 5, 21), to_grade="Outperform", target=270.0),
            _change("Rosenblatt", date(2026, 5, 21), to_grade="Buy", target=325.0),  # unranked
        ),
    )
    info = GetStockAnalystInfo(
        _FakeRecommendationReadProvider(_a_run("NVDA")),
        _FakeRatingChangeReadProvider(changes),
        now=datetime(2026, 6, 1, tzinfo=timezone.utc),  # pin the recency window
    ).execute("NVDA")
    assert [f.firm for f in info.top_firms] == ["RBC Capital"]  # Rosenblatt excluded
    assert info.top_firms[0].rating == "Outperform"


def test_analyst_info_top_firms_empty_without_credible_coverage():
    info = GetStockAnalystInfo(
        _FakeRecommendationReadProvider(_a_run("X")),
        _FakeRatingChangeReadProvider(
            AnalystRatingChanges("X", (_change("Rosenblatt", date(2026, 5, 1)),))
        ),
    ).execute("X")
    assert info.top_firms == ()


class _FakeRepo(RecommendationsRepository):
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


class _FakeRatingChangeProvider(RatingChangeProvider):
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
