from datetime import date

import pytest

from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.ownership.insider_transactions.entities import (
    InsiderActivity,
    InsiderTransaction,
)
from app.domains.ownership.insider_transactions.interfaces import InsiderTransactionsAdapter
from app.domains.ownership.insider_transactions.interfaces import (
    InsiderTransactionsRepositoryAdapter,
    RefreshTarget,
)
from app.domains.ownership.insider_transactions.use_cases import (
    GetInsiderTransactions,
    InsiderTransactionsSyncReport,
    SyncInsiderTransactions,
)


def _activity(symbol: str) -> InsiderActivity:
    return InsiderActivity(
        symbol,
        (
            InsiderTransaction(
                filing_date=date(2026, 6, 17),
                transaction_date=date(2026, 6, 15),
                insider_name="Jane Insider",
                officer_title="CEO",
                is_director=False,
                is_officer=True,
                is_ten_percent_owner=False,
                security_title="Common Stock",
                transaction_code="P",
                acquired_disposed="A",
                shares=100.0,
                price_per_share=10.0,
                shares_owned_following=1000.0,
                accession_number="acc-1",
                line_index=0,
            ),
        ),
    )


class _FakeProvider(InsiderTransactionsAdapter):
    def __init__(self, result: InsiderActivity) -> None:
        self.result = result
        self.calls: list[str] = []

    def get_insider_transactions(self, symbol: str) -> InsiderActivity:
        self.calls.append(symbol)
        return self.result


def test_normalizes_symbol_before_calling_the_provider():
    fake = _FakeProvider(InsiderActivity("AAPL"))
    GetInsiderTransactions(fake).execute("  aapl ")
    assert fake.calls == ["AAPL"]  # trimmed + upper-cased once, at the edge


def test_passes_the_activity_through_untouched():
    activity = InsiderActivity("AAPL")
    assert GetInsiderTransactions(_FakeProvider(activity)).execute("AAPL") is activity


@pytest.mark.parametrize("bad", ["", "   ", "123", "TOOLONG"])
def test_rejects_a_bad_symbol_with_value_error(bad):
    fake = _FakeProvider(InsiderActivity("AAPL"))
    with pytest.raises(ValueError):
        GetInsiderTransactions(fake).execute(bad)
    assert fake.calls == []  # never reached the provider


class _FakeRepo(InsiderTransactionsRepositoryAdapter):
    def __init__(self, targets: list[RefreshTarget]) -> None:
        self._targets = list(targets)
        self.upserts: list[tuple[str, str | None]] = []
        self.refresh_limit: int | None = "unset"

    def get(self, symbol: str) -> InsiderActivity | None:
        return None

    def upsert(self, symbol, name, activity) -> None:
        self.upserts.append((symbol, name))

    def refresh_targets(self, limit) -> list[RefreshTarget]:
        self.refresh_limit = limit
        return self._targets if limit is None else self._targets[:limit]


class _FakeSyncProvider(InsiderTransactionsAdapter):
    def __init__(self, *, empty=(), errors=None) -> None:
        self._empty = set(empty)
        self._errors = errors or {}
        self.calls: list[str] = []

    def get_insider_transactions(self, symbol: str) -> InsiderActivity:
        self.calls.append(symbol)
        if symbol in self._errors:
            raise self._errors[symbol]
        if symbol in self._empty:
            return InsiderActivity(symbol)
        return _activity(symbol)


def test_sync_refreshes_every_target_and_reports_counts():
    repo = _FakeRepo([RefreshTarget("GOOGL", "Alphabet"), RefreshTarget("MSFT", None)])
    provider = _FakeSyncProvider()

    report = SyncInsiderTransactions(provider, repo).execute(limit=10)

    assert isinstance(report, InsiderTransactionsSyncReport)
    assert (report.refreshed, report.failed, report.limit) == (2, 0, 10)
    assert provider.calls == ["GOOGL", "MSFT"]  # serial, in stalest order
    assert repo.upserts == [("GOOGL", "Alphabet"), ("MSFT", None)]  # name carried through


def test_sync_counts_failures_and_keeps_going():
    repo = _FakeRepo(
        [RefreshTarget("GOOGL", None), RefreshTarget("BAD", None), RefreshTarget("MSFT", None)]
    )
    provider = _FakeSyncProvider(errors={"BAD": StockDataUnavailable("BAD", "sec down")})

    report = SyncInsiderTransactions(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (2, 1)
    assert [s for s, _ in repo.upserts] == ["GOOGL", "MSFT"]  # BAD skipped, not stored


def test_sync_not_found_is_a_failure_not_a_crash():
    repo = _FakeRepo([RefreshTarget("ZZZZ", None)])
    provider = _FakeSyncProvider(errors={"ZZZZ": StockNotFound("ZZZZ")})

    report = SyncInsiderTransactions(provider, repo).execute()

    assert (report.refreshed, report.failed) == (0, 1)
    assert repo.upserts == []


def test_sync_empty_live_result_is_skipped_not_stored():
    repo = _FakeRepo([RefreshTarget("GOOGL", "Alphabet"), RefreshTarget("GONE", None)])
    provider = _FakeSyncProvider(empty={"GONE"})

    report = SyncInsiderTransactions(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (1, 1)
    assert repo.upserts == [("GOOGL", "Alphabet")]  # GONE never upserted


def test_sync_defaults_to_unlimited_when_no_limit_is_given():
    repo = _FakeRepo([])
    SyncInsiderTransactions(_FakeSyncProvider(), repo).execute()
    assert repo.refresh_limit is None  # None => process every anchor stock (seed + refresh)


def test_sync_limit_is_passed_through_and_floored_at_one():
    repo = _FakeRepo([])
    SyncInsiderTransactions(_FakeSyncProvider(), repo).execute(limit=5)
    assert repo.refresh_limit == 5

    SyncInsiderTransactions(_FakeSyncProvider(), repo).execute(limit=0)
    assert repo.refresh_limit == 1  # a non-positive cap is floored to one
