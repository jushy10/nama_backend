"""Offline tests for the fundamentals slice's use case (SyncFundamentals).

The sync is driven through hand-written fakes for the two ports (the live source and the
persistence repository), so nothing touches Yahoo or SQLAlchemy. Verifies the sweep walks the
repository's stale-first work-list, writes each served snapshot, is best-effort per stock (a
source failure is counted and skipped, never aborting), and skips a served-but-hollow snapshot.
"""

from __future__ import annotations

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.fundamentals.entities import Fundamentals
from app.stocks.fundamentals.repository import FundamentalsRepository, RefreshTarget
from app.stocks.fundamentals.use_cases import SyncFundamentals


class FakeProvider:
    """Returns a canned snapshot per symbol, or raises what the test configured for it."""

    def __init__(self, by_symbol: dict[str, Fundamentals | Exception]) -> None:
        self._by_symbol = by_symbol
        self.requested: list[str] = []

    def get_fundamentals(self, symbol: str) -> Fundamentals:
        self.requested.append(symbol)
        result = self._by_symbol.get(symbol, Fundamentals())
        if isinstance(result, Exception):
            raise result
        return result


class FakeRepo(FundamentalsRepository):
    """Serves a fixed work-list and records every upsert (symbol, name, snapshot)."""

    def __init__(self, targets: list[RefreshTarget]) -> None:
        self._targets = targets
        self.requested_limit: int | None = "unset"  # type: ignore[assignment]
        self.upserts: list[tuple[str, str | None, Fundamentals]] = []

    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        self.requested_limit = limit
        return list(self._targets)

    def upsert(self, symbol: str, name: str | None, fundamentals: Fundamentals) -> None:
        self.upserts.append((symbol, name, fundamentals))


def _fundamentals(**overrides) -> Fundamentals:
    base = dict(gross_margin=44.0, net_margin=25.0, beta=1.2)
    base.update(overrides)
    return Fundamentals(**base)


def test_sync_writes_each_served_snapshot_and_passes_the_limit_through():
    targets = [RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("MSFT", "Microsoft")]
    provider = FakeProvider(
        {"AAPL": _fundamentals(net_margin=25.0), "MSFT": _fundamentals(net_margin=36.0)}
    )
    repo = FakeRepo(targets)

    report = SyncFundamentals(provider, repo).execute(limit=10)

    assert repo.requested_limit == 10  # the cap reached the work-list read
    assert provider.requested == ["AAPL", "MSFT"]  # walked in the stale-first order given
    assert [(s, n) for s, n, _ in repo.upserts] == [("AAPL", "Apple Inc."), ("MSFT", "Microsoft")]
    assert repo.upserts[1][2].net_margin == 36.0  # the served figures were written
    assert (report.refreshed, report.failed, report.limit) == (2, 0, 10)


def test_sync_is_best_effort_a_source_failure_is_counted_and_skipped():
    targets = [RefreshTarget("AAPL", None), RefreshTarget("BLOCKED", None), RefreshTarget("MSFT", None)]
    provider = FakeProvider(
        {
            "AAPL": _fundamentals(),
            "BLOCKED": StockDataUnavailable("BLOCKED", "IP gate"),
            "MSFT": _fundamentals(),
        }
    )
    repo = FakeRepo(targets)

    report = SyncFundamentals(provider, repo).execute()

    # The blocked stock is skipped (not written) and the sweep carries on to the next.
    assert [s for s, _, _ in repo.upserts] == ["AAPL", "MSFT"]
    assert (report.refreshed, report.failed) == (2, 1)
    assert report.limit is None  # omitted -> the whole anchor


def test_sync_counts_an_uncovered_symbol_as_failed():
    provider = FakeProvider({"NOPE": StockNotFound("NOPE")})
    repo = FakeRepo([RefreshTarget("NOPE", None)])

    report = SyncFundamentals(provider, repo).execute()

    assert repo.upserts == []  # nothing written
    assert (report.refreshed, report.failed) == (0, 1)


def test_sync_skips_a_served_but_hollow_snapshot():
    # A reachable .info that carried no figure at all -> is_empty -> not stamped, so a later
    # sweep retries it rather than freezing it as "fresh".
    provider = FakeProvider({"THIN": Fundamentals()})
    repo = FakeRepo([RefreshTarget("THIN", None)])

    report = SyncFundamentals(provider, repo).execute()

    assert repo.upserts == []
    assert (report.refreshed, report.failed) == (0, 0)  # neither written nor a failure


def test_sync_over_an_empty_worklist_is_a_noop():
    provider = FakeProvider({})
    repo = FakeRepo([])

    report = SyncFundamentals(provider, repo).execute()

    assert provider.requested == []
    assert (report.refreshed, report.failed) == (0, 0)
