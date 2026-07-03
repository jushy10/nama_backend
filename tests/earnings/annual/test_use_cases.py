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
    """Serves a fixed target list (and optional stored timelines) and records upserts."""

    def __init__(
        self,
        targets: list[RefreshTarget],
        stored: dict[str, AnnualEarningsTimeline] | None = None,
    ) -> None:
        self._targets = list(targets)
        self._stored = stored or {}
        self.upserts: list[tuple[str, str | None]] = []
        self.saved: dict[str, AnnualEarningsTimeline] = {}
        self.refresh_limit: int | None = None

    def get(self, symbol: str) -> AnnualEarningsTimeline | None:
        return self._stored.get(symbol)

    def upsert(self, symbol, name, timeline) -> None:
        self.upserts.append((symbol, name))
        self.saved[symbol] = timeline

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


def _reported(
    year: int, eps: float, revenue: float | None = 400e9, consensus: float | None = None
) -> AnnualEarnings:
    return AnnualEarnings(
        fiscal_year=year, period_end=date(year, 12, 31), eps_actual=eps,
        eps_estimate=None, revenue_actual=revenue, revenue_estimate=None,
        net_income=100e9, eps_actual_consensus=consensus,
    )


def _upcoming(year: int, eps: float) -> AnnualEarnings:
    return AnnualEarnings(
        fiscal_year=year, period_end=date(year, 12, 31), eps_actual=None,
        eps_estimate=eps, revenue_actual=None, revenue_estimate=500e9,
    )


class _TimelineSyncProvider(AnnualEarningsProvider):
    """Returns one canned timeline regardless of symbol."""

    def __init__(self, timeline: AnnualEarningsTimeline) -> None:
        self._timeline = timeline

    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        return self._timeline


def test_sync_degraded_fetch_keeps_stored_reported_years():
    # Yahoo IP-gates the income-statement endpoint: a blocked fetch yields a
    # forward-only timeline. The refresh must merge it with the stored rows rather
    # than overwrite the reported history with nothing.
    stored = AnnualEarningsTimeline(
        "AAPL", (_reported(2024, 6.0), _reported(2025, 7.3), _upcoming(2026, 8.0))
    )
    fresh = AnnualEarningsTimeline("AAPL", (_upcoming(2026, 8.1), _upcoming(2027, 9.2)))
    repo = _FakeRepo([RefreshTarget("AAPL", None)], stored={"AAPL": stored})

    SyncAnnualEarnings(_TimelineSyncProvider(fresh), repo).execute()

    saved = repo.saved["AAPL"]
    assert [y.fiscal_year for y in saved.past] == [2024, 2025]  # history retained
    assert [y.fiscal_year for y in saved.future] == [2026, 2027]
    assert saved.future[0].eps_estimate == 8.1  # fresh consensus still wins


def test_sync_normal_roll_does_not_grow_the_reported_window():
    # A new reported year rolls the oldest one off — retention protects against
    # outages without accumulating history run over run.
    stored = AnnualEarningsTimeline(
        "AAPL",
        tuple(_reported(y, 5.0) for y in (2021, 2022, 2023, 2024)),
    )
    fresh = AnnualEarningsTimeline(
        "AAPL",
        tuple(_reported(y, 6.0) for y in (2022, 2023, 2024, 2025)),
    )
    repo = _FakeRepo([RefreshTarget("AAPL", None)], stored={"AAPL": stored})

    SyncAnnualEarnings(_TimelineSyncProvider(fresh), repo).execute()

    saved = repo.saved["AAPL"]
    assert [y.fiscal_year for y in saved.past] == [2022, 2023, 2024, 2025]
    assert saved.past[0].eps_actual == 6.0  # the fresh figures, not the stored ones


def test_sync_degraded_fetch_fills_missing_consensus_actual_from_stored():
    # The consensus-basis annual actual rides on the announcement history, fetched
    # separately from the income statement — a refresh can return a reported year without
    # it. The stored figure is carried forward; a fresh one wins when present.
    stored = AnnualEarningsTimeline(
        "AAPL", (_reported(2024, 6.0, consensus=6.4), _reported(2025, 7.3, consensus=7.8))
    )
    fresh = AnnualEarningsTimeline(
        "AAPL", (_reported(2024, 6.0), _reported(2025, 7.3, consensus=7.9))
    )
    repo = _FakeRepo([RefreshTarget("AAPL", None)], stored={"AAPL": stored})

    SyncAnnualEarnings(_TimelineSyncProvider(fresh), repo).execute()

    saved = {y.fiscal_year: y for y in repo.saved["AAPL"].years}
    assert saved[2024].eps_actual_consensus == 6.4  # stored fills the fresh hole
    assert saved[2025].eps_actual_consensus == 7.9  # fresh wins when present


def test_sync_reported_year_never_downgrades_to_upcoming():
    # Yahoo's estimate frames lag a fresh report: the fetch may still list a year
    # as upcoming that the store already holds as reported. The published actual wins.
    stored = AnnualEarningsTimeline("AAPL", (_reported(2025, 7.3),))
    fresh = AnnualEarningsTimeline("AAPL", (_upcoming(2025, 7.1), _upcoming(2026, 8.0)))
    repo = _FakeRepo([RefreshTarget("AAPL", None)], stored={"AAPL": stored})

    SyncAnnualEarnings(_TimelineSyncProvider(fresh), repo).execute()

    saved = repo.saved["AAPL"]
    year_2025 = next(y for y in saved.years if y.fiscal_year == 2025)
    assert year_2025.is_reported and year_2025.eps_actual == 7.3


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
