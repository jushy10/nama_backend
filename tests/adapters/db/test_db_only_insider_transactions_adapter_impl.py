from datetime import date

from app.stocks.adapters.db.db_only_insider_transactions_adapter_impl import (
    InsiderTransactionsAdapterImpl,
)
from app.stocks.company.insider_transactions.entities import (
    InsiderActivity,
    InsiderTransaction,
)
from app.stocks.company.insider_transactions.interfaces import (
    InsiderTransactionsRepositoryAdapter,
    RefreshTarget,
)


def _txn(key: str) -> InsiderTransaction:
    return InsiderTransaction(
        filing_date=date(2026, 6, 17),
        transaction_date=date(2026, 6, 15),
        insider_name="Jane",
        officer_title="CEO",
        is_director=False,
        is_officer=True,
        is_ten_percent_owner=False,
        security_title="Common Stock",
        transaction_code="P",
        acquired_disposed="A",
        shares=100.0,
        price_per_share=1.0,
        shares_owned_following=None,
        accession_number=key,
        line_index=0,
    )


class FakeRepo(InsiderTransactionsRepositoryAdapter):
    def __init__(self, stored: InsiderActivity | None = None) -> None:
        self._stored = stored
        self.fail_get = False

    def get(self, symbol: str) -> InsiderActivity | None:
        if self.fail_get:
            raise RuntimeError("db read down")
        return self._stored

    def upsert(self, symbol, name, activity) -> None:  # never called on a read
        raise AssertionError("DB-only read must not write")

    def refresh_targets(self, limit) -> list[RefreshTarget]:  # unused here
        return []


def test_serves_the_stored_feed():
    stored = InsiderActivity("AAPL", (_txn("a"), _txn("b")))
    out = InsiderTransactionsAdapterImpl(FakeRepo(stored)).get_insider_transactions("AAPL")
    assert out is stored  # passed straight through, untouched


def test_miss_returns_empty_and_never_fetches_live():
    out = InsiderTransactionsAdapterImpl(FakeRepo(None)).get_insider_transactions("ZZZZ")
    assert out.symbol == "ZZZZ" and out.is_empty  # empty, not a live fetch, not an error


def test_read_error_degrades_to_empty():
    repo = FakeRepo(InsiderActivity("AAPL", (_txn("a"),)))
    repo.fail_get = True
    out = InsiderTransactionsAdapterImpl(repo).get_insider_transactions("AAPL")
    assert out.symbol == "AAPL" and out.is_empty  # a DB hiccup reads empty, never 500s
