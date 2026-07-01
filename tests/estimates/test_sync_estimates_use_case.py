"""Tests for the SyncAnalystEstimates use case.

Offline: hand-written fakes for the provider and repository ports, so this exercises
only the orchestration — which targets are refreshed and in what order, how per-symbol
failures are counted without aborting the run, that the stored name is carried through
to the upsert, and how the per-run limit is applied — independent of FMP or the DB.
"""

from datetime import date

from app.stocks.entities import AnalystEstimates
from app.stocks.estimates.estimates_ports import (
    AnalystEstimatesProvider,
    AnalystEstimatesRepository,
    CachedEstimates,
    RefreshTarget,
)
from app.stocks.estimates.use_cases import EstimatesSyncReport, SyncAnalystEstimates
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


def _est(eps_avg: float = 8.0) -> AnalystEstimates:
    return AnalystEstimates(
        fiscal_year=2026, period_end=date(2026, 9, 30), eps_avg=eps_avg, eps_low=None,
        eps_high=None, revenue_avg=400e9, num_analysts_eps=10, num_analysts_revenue=10,
    )


class FakeRepo(AnalystEstimatesRepository):
    """Serves a fixed target list and records what got upserted."""

    def __init__(self, targets: list[RefreshTarget]) -> None:
        self._targets = list(targets)
        self.upserts: list[tuple[str, str | None]] = []
        self.refresh_limit: int | None = None

    def get(self, symbol: str) -> CachedEstimates | None:  # unused here
        return None

    def upsert(self, symbol: str, name: str | None, estimates: AnalystEstimates) -> None:
        self.upserts.append((symbol, name))

    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        self.refresh_limit = limit
        return self._targets[:limit]


class FakeProvider(AnalystEstimatesProvider):
    """Returns a canned estimate per symbol, or raises the configured error."""

    def __init__(self, *, errors: dict[str, Exception] | None = None) -> None:
        self._errors = errors or {}
        self.calls: list[str] = []

    def get_estimates(self, symbol: str) -> AnalystEstimates:
        self.calls.append(symbol)
        if symbol in self._errors:
            raise self._errors[symbol]
        return _est()


def test_refreshes_every_target_and_reports_counts():
    repo = FakeRepo([RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("MSFT", None)])
    provider = FakeProvider()

    report = SyncAnalystEstimates(provider, repo).execute(limit=10)

    assert isinstance(report, EstimatesSyncReport)
    assert (report.refreshed, report.failed, report.limit) == (2, 0, 10)
    assert provider.calls == ["AAPL", "MSFT"]  # walked in stalest-first order
    assert repo.upserts == [("AAPL", "Apple Inc."), ("MSFT", None)]


def test_carries_the_stored_name_through_to_upsert():
    # The name rides the stocks anchor; a nameless refresh must not drop a known one,
    # so the use case passes the target's stored name straight to upsert.
    repo = FakeRepo([RefreshTarget("AAPL", "Apple Inc.")])
    SyncAnalystEstimates(FakeProvider(), repo).execute()
    assert repo.upserts == [("AAPL", "Apple Inc.")]


def test_counts_failures_and_keeps_going():
    repo = FakeRepo(
        [RefreshTarget("AAPL", None), RefreshTarget("BAD", None), RefreshTarget("MSFT", None)]
    )
    provider = FakeProvider(errors={"BAD": StockDataUnavailable("BAD", "FMP down")})

    report = SyncAnalystEstimates(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (2, 1)
    assert [s for s, _ in repo.upserts] == ["AAPL", "MSFT"]  # BAD skipped, not stored


def test_not_found_is_a_failure_not_a_crash():
    repo = FakeRepo([RefreshTarget("ZZZZ", None)])
    provider = FakeProvider(errors={"ZZZZ": StockNotFound("ZZZZ")})

    report = SyncAnalystEstimates(provider, repo).execute()

    assert (report.refreshed, report.failed) == (0, 1)
    assert repo.upserts == []


def test_default_limit_is_applied_when_unspecified():
    repo = FakeRepo([])
    SyncAnalystEstimates(FakeProvider(), repo).execute()
    assert repo.refresh_limit == SyncAnalystEstimates.DEFAULT_LIMIT


def test_limit_is_passed_through_and_floored_at_one():
    repo = FakeRepo([])
    SyncAnalystEstimates(FakeProvider(), repo).execute(limit=5)
    assert repo.refresh_limit == 5

    SyncAnalystEstimates(FakeProvider(), repo).execute(limit=0)
    assert repo.refresh_limit == 1  # a non-positive cap is floored to one row
