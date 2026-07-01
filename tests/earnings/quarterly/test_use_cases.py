"""Tests for the quarterly-earnings use cases: GetQuarterlyEarnings + SyncQuarterlyEarnings.

Offline: hand-written fakes for the provider and repository ports, so this exercises only
the orchestration — symbol normalization and timeline pass-through on the read side; which
targets are refreshed, in what order, failure/empty handling, and the per-run limit on the
sync side — independent of yfinance or the DB.
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


def test_sync_default_limit_is_applied_when_unspecified():
    repo = _FakeRepo([])
    SyncQuarterlyEarnings(_FakeSyncProvider(), repo).execute()
    assert repo.refresh_limit == SyncQuarterlyEarnings.DEFAULT_LIMIT


def test_sync_limit_is_passed_through_and_floored_at_one():
    repo = _FakeRepo([])
    SyncQuarterlyEarnings(_FakeSyncProvider(), repo).execute(limit=5)
    assert repo.refresh_limit == 5

    SyncQuarterlyEarnings(_FakeSyncProvider(), repo).execute(limit=0)
    assert repo.refresh_limit == 1  # a non-positive cap is floored to one
