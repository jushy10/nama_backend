"""Tests for the SyncQuarterlyEarnings use case.

Offline: hand-written fakes for the provider and repository ports, so this exercises only
the orchestration — which targets are refreshed and in what order, how per-symbol failures
are counted without aborting the run, that an *empty* live result is skipped (not upserted)
so it can't wipe stored history, that the stored name is carried through, and how the
per-run limit is applied — independent of yfinance or the DB.
"""

from datetime import date

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
    QuarterlyEarningsSyncReport,
    SyncQuarterlyEarnings,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


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


class FakeRepo(QuarterlyEarningsRepository):
    """Serves a fixed target list and records what got upserted."""

    def __init__(self, targets: list[RefreshTarget]) -> None:
        self._targets = list(targets)
        self.upserts: list[tuple[str, str | None]] = []
        self.refresh_limit: int | None = None

    def get(self, symbol: str) -> QuarterlyEarningsTimeline | None:  # unused here
        return None

    def upsert(self, symbol, name, timeline) -> None:
        self.upserts.append((symbol, name))

    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        self.refresh_limit = limit
        return self._targets[:limit]


class FakeProvider(QuarterlyEarningsProvider):
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


def test_refreshes_every_target_and_reports_counts():
    repo = FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("MSFT", None)])
    provider = FakeProvider()

    report = SyncQuarterlyEarnings(provider, repo).execute(limit=10)

    assert isinstance(report, QuarterlyEarningsSyncReport)
    assert (report.refreshed, report.failed, report.limit) == (2, 0, 10)
    assert provider.calls == ["AAPL", "MSFT"]  # stalest-first order
    assert repo.upserts == [("AAPL", "Apple Inc."), ("MSFT", None)]


def test_carries_the_stored_name_through_to_upsert():
    repo = FakeRepo([RefreshTarget("AAPL", "Apple Inc.")])
    SyncQuarterlyEarnings(FakeProvider(), repo).execute()
    assert repo.upserts == [("AAPL", "Apple Inc.")]


def test_counts_failures_and_keeps_going():
    repo = FakeRepo(
        [RefreshTarget("AAPL", None), RefreshTarget("BAD", None), RefreshTarget("MSFT", None)]
    )
    provider = FakeProvider(errors={"BAD": StockDataUnavailable("BAD", "yahoo down")})

    report = SyncQuarterlyEarnings(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (2, 1)
    assert [s for s, _ in repo.upserts] == ["AAPL", "MSFT"]  # BAD skipped, not stored


def test_not_found_is_a_failure_not_a_crash():
    repo = FakeRepo([RefreshTarget("ZZZZ", None)])
    provider = FakeProvider(errors={"ZZZZ": StockNotFound("ZZZZ")})

    report = SyncQuarterlyEarnings(provider, repo).execute()

    assert (report.refreshed, report.failed) == (0, 1)
    assert repo.upserts == []


def test_empty_live_result_is_skipped_not_stored():
    # An empty upsert would delete the stored window, so an empty live result must be
    # skipped (and counted as a failure) rather than persisted — the divergence from the
    # estimates sync, which can safely store an empty single row.
    repo = FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("GONE", None)])
    provider = FakeProvider(empty={"GONE"})

    report = SyncQuarterlyEarnings(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (1, 1)
    assert repo.upserts == [("AAPL", "Apple Inc.")]  # GONE never upserted


def test_default_limit_is_applied_when_unspecified():
    repo = FakeRepo([])
    SyncQuarterlyEarnings(FakeProvider(), repo).execute()
    assert repo.refresh_limit == SyncQuarterlyEarnings.DEFAULT_LIMIT


def test_limit_is_passed_through_and_floored_at_one():
    repo = FakeRepo([])
    SyncQuarterlyEarnings(FakeProvider(), repo).execute(limit=5)
    assert repo.refresh_limit == 5

    SyncQuarterlyEarnings(FakeProvider(), repo).execute(limit=0)
    assert repo.refresh_limit == 1  # a non-positive cap is floored to one
