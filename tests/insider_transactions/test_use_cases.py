"""Tests for the insider-transactions use case (GetInsiderTransactions).

Offline: a hand-written fake standing in for the provider port, so this checks only the use
case's own behaviour — symbol normalization at the edge and pass-through to the provider —
without touching SEC or the database.
"""

import pytest

from app.stocks.insider_transactions.entities import InsiderActivity
from app.stocks.insider_transactions.ports import InsiderTransactionsProvider
from app.stocks.insider_transactions.use_cases import GetInsiderTransactions


class _FakeProvider(InsiderTransactionsProvider):
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
