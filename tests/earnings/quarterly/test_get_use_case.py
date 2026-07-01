"""Tests for the GetQuarterlyEarnings read use case.

Offline: a hand-written fake provider, so this exercises only the orchestration — symbol
normalization at the edge and pass-through of the timeline — independent of yfinance or
the DB.
"""

import pytest

from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.earnings.quarterly.use_cases import GetQuarterlyEarnings


class FakeProvider(QuarterlyEarningsProvider):
    def __init__(self, timeline: QuarterlyEarningsTimeline) -> None:
        self._timeline = timeline
        self.calls: list[str] = []

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        self.calls.append(symbol)
        return self._timeline


def test_normalizes_the_symbol_before_calling_the_provider():
    timeline = QuarterlyEarningsTimeline("AAPL", ())
    provider = FakeProvider(timeline)

    out = GetQuarterlyEarnings(provider).execute("  aapl ")

    assert out is timeline
    assert provider.calls == ["AAPL"]  # trimmed + upper-cased once, at the edge


def test_rejects_a_blank_symbol():
    provider = FakeProvider(QuarterlyEarningsTimeline("", ()))
    with pytest.raises(ValueError):
        GetQuarterlyEarnings(provider).execute("   ")
    assert provider.calls == []  # rejected before the provider is touched


def test_rejects_obviously_invalid_symbols():
    provider = FakeProvider(QuarterlyEarningsTimeline("", ()))
    for bad in ("123", "TOOLONG", "BR.K"):
        with pytest.raises(ValueError):
            GetQuarterlyEarnings(provider).execute(bad)
    assert provider.calls == []
