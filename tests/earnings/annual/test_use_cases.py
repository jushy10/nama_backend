"""Tests for the annual-earnings use cases: GetAnnualEarnings + SyncAnnualEarnings.

Offline: hand-written fakes for the provider and repository ports, so this exercises only the
orchestration — symbol normalization and timeline pass-through on the read side; which targets
are refreshed, in what order, failure/empty handling, and the per-run limit on the sync side —
independent of yfinance or the DB.
"""

from datetime import date

import pytest

from app.stocks.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)
from app.stocks.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.earnings.annual.repository import (
    AnnualEarningsRepository,
    RefreshTarget,
)
from app.stocks.earnings.annual.use_cases import (
    AnnualEarningsSyncReport,
    GetAnnualEarnings,
    SyncAnnualEarnings,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


def _a_timeline(symbol: str) -> AnnualEarningsTimeline:
    return AnnualEarningsTimeline(
        symbol=symbol,
        years=(
            AnnualEarnings(
                fiscal_year=2024,
                period_end=date(2024, 12, 31),
                eps_actual=6.0,
                eps_estimate=None,
                revenue_actual=400e9,
                revenue_estimate=None,
                net_income=100e9,
            ),
        ),
    )


# ───────────────────────────── GetAnnualEarnings ─────────────────────────────


class _FakeReadProvider(AnnualEarningsProvider):
    def __init__(self, timeline: AnnualEarningsTimeline) -> None:
        self._timeline = timeline
        self.calls: list[str] = []

    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        self.calls.append(symbol)
        return self._timeline


def test_get_normalizes_the_symbol_before_calling_the_provider():
    timeline = AnnualEarningsTimeline("AAPL", ())
    provider = _FakeReadProvider(timeline)

    out = GetAnnualEarnings(provider).execute("  aapl ")

    assert out is timeline
    assert provider.calls == ["AAPL"]  # trimmed + upper-cased once, at the edge


def test_get_rejects_a_blank_symbol():
    provider = _FakeReadProvider(AnnualEarningsTimeline("", ()))
    with pytest.raises(ValueError):
        GetAnnualEarnings(provider).execute("   ")
    assert provider.calls == []  # rejected before the provider is touched


def test_get_rejects_obviously_invalid_symbols():
    provider = _FakeReadProvider(AnnualEarningsTimeline("", ()))
    for bad in ("123", "TOOLONG", "BR.K"):
        with pytest.raises(ValueError):
            GetAnnualEarnings(provider).execute(bad)
    assert provider.calls == []


# ───────────────────────────── SyncAnnualEarnings ─────────────────────────────


class _FakeRepo(AnnualEarningsRepository):
    """Serves a fixed target list and records what got upserted."""

    def __init__(self, targets: list[RefreshTarget]) -> None:
        self._targets = list(targets)
        self.upserts: list[tuple[str, str | None]] = []
        self.refresh_limit: int | None = None

    def get(self, symbol: str) -> AnnualEarningsTimeline | None:  # unused here
        return None

    def upsert(self, symbol, name, timeline) -> None:
        self.upserts.append((symbol, name))

    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        self.refresh_limit = limit
        return self._targets[:limit]


class _FakeSyncProvider(AnnualEarningsProvider):
    """Returns a canned timeline per symbol, an empty one, or raises."""

    def __init__(self, *, empty=(), errors=None) -> None:
        self._empty = set(empty)
        self._errors = errors or {}
        self.calls: list[str] = []

    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        self.calls.append(symbol)
        if symbol in self._errors:
            raise self._errors[symbol]
        if symbol in self._empty:
            return AnnualEarningsTimeline(symbol, ())
        return _a_timeline(symbol)


def test_sync_refreshes_every_target_and_reports_counts():
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("MSFT", None)])
    provider = _FakeSyncProvider()

    report = SyncAnnualEarnings(provider, repo).execute(limit=10)

    assert isinstance(report, AnnualEarningsSyncReport)
    assert (report.refreshed, report.failed, report.limit) == (2, 0, 10)
    assert provider.calls == ["AAPL", "MSFT"]  # stalest-first order
    assert repo.upserts == [("AAPL", "Apple Inc."), ("MSFT", None)]


def test_sync_carries_the_stored_name_through_to_upsert():
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc.")])
    SyncAnnualEarnings(_FakeSyncProvider(), repo).execute()
    assert repo.upserts == [("AAPL", "Apple Inc.")]


def test_sync_counts_failures_and_keeps_going():
    repo = _FakeRepo(
        [RefreshTarget("AAPL", None), RefreshTarget("BAD", None), RefreshTarget("MSFT", None)]
    )
    provider = _FakeSyncProvider(errors={"BAD": StockDataUnavailable("BAD", "yahoo down")})

    report = SyncAnnualEarnings(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (2, 1)
    assert [s for s, _ in repo.upserts] == ["AAPL", "MSFT"]  # BAD skipped, not stored


def test_sync_not_found_is_a_failure_not_a_crash():
    repo = _FakeRepo([RefreshTarget("ZZZZ", None)])
    provider = _FakeSyncProvider(errors={"ZZZZ": StockNotFound("ZZZZ")})

    report = SyncAnnualEarnings(provider, repo).execute()

    assert (report.refreshed, report.failed) == (0, 1)
    assert repo.upserts == []


def test_sync_empty_live_result_is_skipped_not_stored():
    # An empty upsert would delete the stored window, so an empty live result must be skipped
    # (and counted as a failure) rather than persisted.
    repo = _FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("GONE", None)])
    provider = _FakeSyncProvider(empty={"GONE"})

    report = SyncAnnualEarnings(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (1, 1)
    assert repo.upserts == [("AAPL", "Apple Inc.")]  # GONE never upserted


def test_sync_default_limit_is_applied_when_unspecified():
    repo = _FakeRepo([])
    SyncAnnualEarnings(_FakeSyncProvider(), repo).execute()
    assert repo.refresh_limit == SyncAnnualEarnings.DEFAULT_LIMIT


def test_sync_limit_is_passed_through_and_floored_at_one():
    repo = _FakeRepo([])
    SyncAnnualEarnings(_FakeSyncProvider(), repo).execute(limit=5)
    assert repo.refresh_limit == 5

    SyncAnnualEarnings(_FakeSyncProvider(), repo).execute(limit=0)
    assert repo.refresh_limit == 1  # a non-positive cap is floored to one
