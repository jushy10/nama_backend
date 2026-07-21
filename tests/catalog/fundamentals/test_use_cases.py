from __future__ import annotations

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.catalog.fundamentals.entities import Fundamentals
from app.stocks.catalog.fundamentals.interfaces import FundamentalsRepositoryAdapter, RefreshTarget
from app.stocks.catalog.fundamentals.use_cases import SyncFundamentals


class FakeProvider:
    def __init__(self, by_symbol: dict[str, Fundamentals | Exception]) -> None:
        self._by_symbol = by_symbol
        self.requested: list[str] = []

    def get_fundamentals(self, symbol: str) -> Fundamentals:
        self.requested.append(symbol)
        result = self._by_symbol.get(symbol, Fundamentals())
        if isinstance(result, Exception):
            raise result
        return result


class FakeRepo(FundamentalsRepositoryAdapter):
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


def test_sync_does_not_write_a_served_but_hollow_snapshot():
    # A reachable .info that carried no figure at all -> is_empty -> never stamped (an all-None
    # upsert would wipe nothing but freeze the row as "fresh"), so it's treated as a transient
    # miss to retry, not written.
    provider = FakeProvider({"THIN": Fundamentals()})
    repo = FakeRepo([RefreshTarget("THIN", None)])

    report = SyncFundamentals(provider, repo).execute()

    assert repo.upserts == []
    # The lone hollow read refreshes nothing on the first pass, so the run stops without retrying
    # (a zero-progress pass is a persistent block, not an intermittent one) and counts it failed.
    assert (report.refreshed, report.failed) == (0, 1)


def test_sync_over_an_empty_worklist_is_a_noop():
    provider = FakeProvider({})
    repo = FakeRepo([])

    report = SyncFundamentals(provider, repo).execute()

    assert provider.requested == []
    assert (report.refreshed, report.failed) == (0, 0)


class SequenceProvider:
    def __init__(self, by_symbol: dict[str, list]) -> None:
        self._by_symbol = {k: list(v) for k, v in by_symbol.items()}
        self.requested: list[str] = []

    def get_fundamentals(self, symbol: str) -> Fundamentals:
        self.requested.append(symbol)
        queue = self._by_symbol.get(symbol) or [Fundamentals()]
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, Exception):
            raise result
        return result


def test_a_transient_block_is_recovered_on_a_later_pass():
    # The core of the in-sweep retry: a stock the source gates on the first pass (and the two
    # that succeed prove the gate is intermittent, not total) is re-attempted and written when
    # the gate lifts on the retry — instead of waiting a whole week for the next scheduled run.
    targets = [RefreshTarget("OK1", None), RefreshTarget("GATED", "Gated Co"), RefreshTarget("OK2", None)]
    provider = SequenceProvider(
        {
            "OK1": [_fundamentals()],
            "GATED": [StockDataUnavailable("GATED", "IP gate"), _fundamentals(net_margin=12.0)],
            "OK2": [_fundamentals()],
        }
    )
    repo = FakeRepo(targets)

    report = SyncFundamentals(provider, repo).execute()

    assert provider.requested == ["OK1", "GATED", "OK2", "GATED"]  # only GATED is retried
    assert ("GATED", "Gated Co") in [(s, n) for s, n, _ in repo.upserts]  # written on the retry
    assert (report.refreshed, report.failed) == (3, 0)


def test_a_hollow_read_is_recovered_on_a_later_pass():
    # A served-but-hollow .info (a swallowed crumb-401 the adapter's own retry couldn't clear) is
    # retried like a raised block, and written once the real figures come back.
    targets = [RefreshTarget("OK", None), RefreshTarget("HOLLOW", None)]
    provider = SequenceProvider(
        {"OK": [_fundamentals()], "HOLLOW": [Fundamentals(), _fundamentals(net_margin=9.0)]}
    )
    repo = FakeRepo(targets)

    report = SyncFundamentals(provider, repo).execute()

    assert [s for s, _, _ in repo.upserts] == ["OK", "HOLLOW"]
    assert (report.refreshed, report.failed) == (2, 0)


def test_an_unknown_symbol_is_final_and_never_retried():
    # StockNotFound is genuine no-coverage, so it's counted once and never re-attempted, even
    # when a sibling transient failure keeps the retry passes running.
    targets = [RefreshTarget("OK", None), RefreshTarget("NOPE", None), RefreshTarget("GATED", None)]
    provider = SequenceProvider(
        {
            "OK": [_fundamentals()],
            "NOPE": [StockNotFound("NOPE")],
            "GATED": [StockDataUnavailable("GATED", "gate"), _fundamentals()],
        }
    )
    repo = FakeRepo(targets)

    report = SyncFundamentals(provider, repo).execute()

    assert provider.requested.count("NOPE") == 1  # not retried
    assert provider.requested.count("GATED") == 2  # the transient one is
    assert (report.refreshed, report.failed) == (2, 1)


def test_a_total_block_stops_after_one_pass_without_hammering():
    # When a whole pass recovers nothing, Yahoo is blocking persistently this run — so the sweep
    # stops instead of re-hammering a blocked IP across every attempt (the stragglers wait for
    # the next scheduled run). Each blocked stock is requested exactly once.
    targets = [RefreshTarget("A", None), RefreshTarget("B", None)]
    provider = SequenceProvider(
        {"A": [StockDataUnavailable("A", "gate")], "B": [StockDataUnavailable("B", "gate")]}
    )
    repo = FakeRepo(targets)

    report = SyncFundamentals(provider, repo, max_attempts=3).execute()

    assert provider.requested == ["A", "B"]  # one pass only — no retry when nothing progressed
    assert (report.refreshed, report.failed) == (0, 2)
