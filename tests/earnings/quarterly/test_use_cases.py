"""Tests for the quarterly-earnings use cases: GetQuarterlyEarnings + SyncQuarterlyEarnings.

Offline: hand-written fakes for the provider and repository ports, so this exercises only
the orchestration — symbol normalization and timeline pass-through on the read side; which
targets are refreshed, in what order, failure/empty handling, and the per-run limit on the
sync side — independent of yfinance or the DB. Plus the timeline's pure TTM rule
(``ttm_eps``), which the ticker card's trailing P/E leans on.
"""

from datetime import date

import pytest

from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.earnings.quarterly.repository import (
    QuarterlyEarningsRepository,
    RefreshTarget,
)
from app.stocks.earnings.quarterly.use_cases import (
    GetQuarterlyEarnings,
    QuarterlyEarningsSyncReport,
    SyncQuarterlyEarnings,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.sync_progress import SyncOutcome


def _a_timeline(symbol: str) -> QuarterlyEarningsTimeline:
    return QuarterlyEarningsTimeline(
        symbol=symbol,
        quarters=(
            QuarterlyEarnings(
                fiscal_year=2025,
                fiscal_quarter=4,
                period_end=date(2025, 12, 31),
                report_date=date(2026, 2, 1),
                eps_actual=3.0,
                eps_estimate=2.8,
                eps_surprise=0.2,
                eps_surprise_percent=7.14,
                revenue_estimate=None,
            ),
        ),
    )


# ───────────────────────────── entity rules ─────────────────────────────


def _reported(year: int, quarter: int, eps: float) -> QuarterlyEarnings:
    return QuarterlyEarnings(
        fiscal_year=year, fiscal_quarter=quarter, period_end=None, report_date=None,
        eps_actual=eps, eps_estimate=None, eps_surprise=None,
        eps_surprise_percent=None, revenue_estimate=None,
    )


def _upcoming(year: int, quarter: int) -> QuarterlyEarnings:
    return QuarterlyEarnings(
        fiscal_year=year, fiscal_quarter=quarter, period_end=None, report_date=None,
        eps_actual=None, eps_estimate=2.0, eps_surprise=None,
        eps_surprise_percent=None, revenue_estimate=None,
    )


def test_ttm_eps_sums_the_four_newest_reported_quarters():
    # Five reported quarters: the oldest (1.0) must fall out of the sum, and the
    # upcoming quarter contributes nothing.
    timeline = QuarterlyEarningsTimeline(
        symbol="MU",
        quarters=(
            _reported(2025, 2, 1.0),
            _reported(2025, 3, 1.5),
            _reported(2025, 4, 2.0),
            _reported(2026, 1, 2.5),
            _reported(2026, 2, 3.0),
            _upcoming(2026, 3),
        ),
    )
    assert timeline.ttm_eps == pytest.approx(9.0)  # 1.5 + 2.0 + 2.5 + 3.0


def test_ttm_eps_is_none_with_fewer_than_four_reported_quarters():
    # A partial sum understates the year — three reported quarters (plus an
    # upcoming one) must not masquerade as a TTM figure.
    timeline = QuarterlyEarningsTimeline(
        symbol="MU",
        quarters=(
            _reported(2025, 4, 2.0),
            _reported(2026, 1, 2.5),
            _reported(2026, 2, 3.0),
            _upcoming(2026, 3),
        ),
    )
    assert timeline.ttm_eps is None
    assert QuarterlyEarningsTimeline("MU", ()).ttm_eps is None


# ───────────────────────────── GetQuarterlyEarnings ─────────────────────────────


class _FakeReadProvider(QuarterlyEarningsProvider):
    def __init__(self, timeline: QuarterlyEarningsTimeline) -> None:
        self._timeline = timeline
        self.calls: list[str] = []

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        self.calls.append(symbol)
        return self._timeline


def test_get_normalizes_the_symbol_before_calling_the_provider():
    timeline = QuarterlyEarningsTimeline("AAPL", ())
    provider = _FakeReadProvider(timeline)

    out = GetQuarterlyEarnings(provider).execute("  aapl ")

    assert out is timeline
    assert provider.calls == ["AAPL"]  # trimmed + upper-cased once, at the edge


def test_get_rejects_a_blank_symbol():
    provider = _FakeReadProvider(QuarterlyEarningsTimeline("", ()))
    with pytest.raises(ValueError):
        GetQuarterlyEarnings(provider).execute("   ")
    assert provider.calls == []  # rejected before the provider is touched


def test_get_rejects_obviously_invalid_symbols():
    provider = _FakeReadProvider(QuarterlyEarningsTimeline("", ()))
    for bad in ("123", "TOOLONG", "BR.K"):
        with pytest.raises(ValueError):
            GetQuarterlyEarnings(provider).execute(bad)
    assert provider.calls == []


# ───────────────────────────── SyncQuarterlyEarnings ─────────────────────────────


class _FakeRepo(QuarterlyEarningsRepository):
    """Serves a fixed target list (and optional stored timelines) and records upserts."""

    def __init__(
        self,
        targets: list[RefreshTarget],
        stored: dict[str, QuarterlyEarningsTimeline] | None = None,
    ) -> None:
        self._targets = list(targets)
        self._stored = stored or {}
        self.upserts: list[tuple[str, str | None]] = []
        self.saved: dict[str, QuarterlyEarningsTimeline] = {}
        self.refresh_limit: int | None = None

    def get(self, symbol: str) -> QuarterlyEarningsTimeline | None:
        return self._stored.get(symbol)

    def upsert(self, symbol, name, timeline) -> None:
        self.upserts.append((symbol, name))
        self.saved[symbol] = timeline

    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        self.refresh_limit = limit
        return self._targets[:limit]


class _FakeSyncProvider(QuarterlyEarningsProvider):
    """Returns a canned timeline per symbol, an empty one, or raises."""

    def __init__(self, *, empty=(), errors=None) -> None:
        self._empty = set(empty)
        self._errors = errors or {}
        self.calls: list[str] = []

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        self.calls.append(symbol)
        if symbol in self._errors:
            raise self._errors[symbol]
        if symbol in self._empty:
            return QuarterlyEarningsTimeline(symbol, ())
        return _a_timeline(symbol)


def test_sync_refreshes_every_target_and_reports_counts():
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("MSFT", None)])
    provider = _FakeSyncProvider()

    report = SyncQuarterlyEarnings(provider, repo).execute(limit=10)

    assert isinstance(report, QuarterlyEarningsSyncReport)
    assert (report.refreshed, report.failed, report.limit) == (2, 0, 10)
    assert provider.calls == ["AAPL", "MSFT"]  # stalest-first order
    assert repo.upserts == [("AAPL", "Apple Inc."), ("MSFT", None)]


def test_sync_carries_the_stored_name_through_to_upsert():
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc.")])
    SyncQuarterlyEarnings(_FakeSyncProvider(), repo).execute()
    assert repo.upserts == [("AAPL", "Apple Inc.")]


def test_sync_counts_failures_and_keeps_going():
    repo = _FakeRepo(
        [RefreshTarget("AAPL", None), RefreshTarget("BAD", None), RefreshTarget("MSFT", None)]
    )
    provider = _FakeSyncProvider(errors={"BAD": StockDataUnavailable("BAD", "yahoo down")})

    report = SyncQuarterlyEarnings(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (2, 1)
    assert [s for s, _ in repo.upserts] == ["AAPL", "MSFT"]  # BAD skipped, not stored


def test_sync_not_found_is_a_failure_not_a_crash():
    repo = _FakeRepo([RefreshTarget("ZZZZ", None)])
    provider = _FakeSyncProvider(errors={"ZZZZ": StockNotFound("ZZZZ")})

    report = SyncQuarterlyEarnings(provider, repo).execute()

    assert (report.refreshed, report.failed) == (0, 1)
    assert repo.upserts == []


def test_sync_empty_live_result_is_skipped_not_stored():
    # An empty upsert would delete the stored window, so an empty live result must be
    # skipped (and counted as a failure) rather than persisted.
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("GONE", None)])
    provider = _FakeSyncProvider(empty={"GONE"})

    report = SyncQuarterlyEarnings(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (1, 1)
    assert repo.upserts == [("AAPL", "Apple Inc.")]  # GONE never upserted



def _reported_q(
    year: int, quarter: int, eps: float, revenue: float | None
) -> QuarterlyEarnings:
    return QuarterlyEarnings(
        fiscal_year=year, fiscal_quarter=quarter,
        period_end=date(year, quarter * 3, 28), report_date=None,
        eps_actual=eps, eps_estimate=eps - 0.1, eps_surprise=0.1,
        eps_surprise_percent=5.0, revenue_estimate=None, revenue_actual=revenue,
    )


def _upcoming_q(year: int, quarter: int, eps: float) -> QuarterlyEarnings:
    return QuarterlyEarnings(
        fiscal_year=year, fiscal_quarter=quarter,
        period_end=date(year, quarter * 3, 28), report_date=None,
        eps_actual=None, eps_estimate=eps, eps_surprise=None,
        eps_surprise_percent=None, revenue_estimate=90e9, revenue_actual=None,
    )


class _TimelineSyncProvider(QuarterlyEarningsProvider):
    """Returns one canned timeline regardless of symbol."""

    def __init__(self, timeline: QuarterlyEarningsTimeline) -> None:
        self._timeline = timeline

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        return self._timeline


def test_sync_fills_missing_revenue_from_the_stored_rows():
    # Yahoo IP-gates the income-statement (revenue) endpoint hardest: a refresh may
    # come back with EPS but no revenue. The stored revenue must survive the rewrite.
    stored = QuarterlyEarningsTimeline(
        "AAPL", (_reported_q(2026, 1, 2.0, revenue=100e9),)
    )
    fresh = QuarterlyEarningsTimeline(
        "AAPL",
        (_reported_q(2026, 1, 2.0, revenue=None), _upcoming_q(2026, 2, 2.1)),
    )
    repo = _FakeRepo([RefreshTarget("AAPL", None)], stored={"AAPL": stored})

    SyncQuarterlyEarnings(_TimelineSyncProvider(fresh), repo).execute()

    saved = repo.saved["AAPL"]
    assert saved.past[0].revenue_actual == 100e9  # carried forward, not nulled
    assert [q.fiscal_quarter for q in saved.future] == [2]  # fresh forward kept


def test_sync_degraded_fetch_keeps_stored_reported_quarters():
    stored = QuarterlyEarningsTimeline(
        "AAPL",
        (_reported_q(2025, 4, 1.8, 95e9), _reported_q(2026, 1, 2.0, 100e9)),
    )
    fresh = QuarterlyEarningsTimeline("AAPL", (_upcoming_q(2026, 2, 2.1),))
    repo = _FakeRepo([RefreshTarget("AAPL", None)], stored={"AAPL": stored})

    SyncQuarterlyEarnings(_TimelineSyncProvider(fresh), repo).execute()

    saved = repo.saved["AAPL"]
    assert [(q.fiscal_year, q.fiscal_quarter) for q in saved.past] == [
        (2025, 4),
        (2026, 1),
    ]


def test_sync_normal_roll_does_not_grow_the_reported_window():
    stored = QuarterlyEarningsTimeline(
        "AAPL", tuple(_reported_q(2025, q, 1.5, 90e9) for q in (1, 2, 3, 4))
    )
    fresh = QuarterlyEarningsTimeline(
        "AAPL",
        tuple(_reported_q(2025, q, 1.6, 92e9) for q in (2, 3, 4))
        + (_reported_q(2026, 1, 2.0, 100e9),),
    )
    repo = _FakeRepo([RefreshTarget("AAPL", None)], stored={"AAPL": stored})

    SyncQuarterlyEarnings(_TimelineSyncProvider(fresh), repo).execute()

    saved = repo.saved["AAPL"]
    assert [(q.fiscal_year, q.fiscal_quarter) for q in saved.past] == [
        (2025, 2),
        (2025, 3),
        (2025, 4),
        (2026, 1),
    ]  # (2025, 1) rolled off; the window stayed four reported quarters


def test_sync_reports_progress_once_per_stock_with_outcomes():
    # on_progress fires once per target — OK, a vendor failure, then an empty result — each
    # carrying its 1-based position and the run total. Pure observation; counts are unchanged.
    repo = _FakeRepo(
        [
            RefreshTarget("AAPL", None),
            RefreshTarget("BAD", None),
            RefreshTarget("GONE", None),
        ]
    )
    provider = _FakeSyncProvider(
        errors={"BAD": StockDataUnavailable("BAD", "yahoo down")}, empty={"GONE"}
    )
    ticks = []

    report = SyncQuarterlyEarnings(provider, repo).execute(
        limit=10, on_progress=ticks.append
    )

    assert (report.refreshed, report.failed) == (1, 2)
    assert [(t.done, t.total, t.symbol, t.outcome) for t in ticks] == [
        (1, 3, "AAPL", SyncOutcome.OK),
        (2, 3, "BAD", SyncOutcome.FAILED),
        (3, 3, "GONE", SyncOutcome.FAILED),
    ]


def test_sync_defaults_to_unlimited_when_no_limit_is_given():
    repo = _FakeRepo([])
    SyncQuarterlyEarnings(_FakeSyncProvider(), repo).execute()
    assert repo.refresh_limit is None  # None => process every anchor stock (seed + refresh)


def test_sync_limit_is_passed_through_and_floored_at_one():
    repo = _FakeRepo([])
    SyncQuarterlyEarnings(_FakeSyncProvider(), repo).execute(limit=5)
    assert repo.refresh_limit == 5

    SyncQuarterlyEarnings(_FakeSyncProvider(), repo).execute(limit=0)
    assert repo.refresh_limit == 1  # a non-positive cap is floored to one
