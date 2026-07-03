"""Tests for the ticker use case: GetTickerValuation.

Offline: hand-written fakes for the quote and estimates ports, so this exercises only
the orchestration — symbol normalization, assembling the valuation from the live price
+ stored consensus, and the "no coverage ≠ error" stance — plus the entity rule the
response leans on (the forward-PEG guard), independent of Alpaca or the DB.
"""

from datetime import datetime, timezone

import pytest

from app.stocks.entities import AnalystEstimates, Quote
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import AnalystEstimatesProvider, StockQuoteProvider
from app.stocks.ticker.entities import TickerValuation
from app.stocks.ticker.use_cases import GetTickerValuation

_EMPTY = AnalystEstimates(
    fiscal_year=None, period_end=None, eps_avg=None, revenue_avg=None
)


def _a_quote(symbol: str, price: float) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        previous_close=None,
        bid=None,
        ask=None,
        as_of=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )


def _estimates(eps_avg=None, eps_avg_fy2=None):
    return AnalystEstimates(
        fiscal_year=2026,
        period_end=None,
        eps_avg=eps_avg,
        revenue_avg=None,
        fiscal_year_fy2=2027,
        eps_avg_fy2=eps_avg_fy2,
    )


class _FakeQuotes(StockQuoteProvider):
    def __init__(self, price: float = 100.0, error: Exception | None = None) -> None:
        self._price = price
        self._error = error
        self.calls: list[str] = []

    def get_quote(self, symbol: str) -> Quote:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return _a_quote(symbol, self._price)


class _FakeEstimates(AnalystEstimatesProvider):
    def __init__(self, estimates: AnalystEstimates = _EMPTY) -> None:
        self._estimates = estimates
        self.calls: list[str] = []

    def get_estimates(self, symbol: str) -> AnalystEstimates:
        self.calls.append(symbol)
        return self._estimates


# ───────────────────────────── entity rules ─────────────────────────────


def _a_valuation(forward_pe, forward_eps_growth) -> TickerValuation:
    return TickerValuation(
        symbol="MU",
        price=100.0,
        forward_pe=forward_pe,
        forward_eps_growth=forward_eps_growth,
    )


def test_forward_peg_is_the_ratio_of_the_two_legs():
    # The MU shape: a 13.3 multiple against 104.1% expected growth.
    assert _a_valuation(13.3, 104.1).forward_peg == 0.13


@pytest.mark.parametrize(
    "pe, growth",
    [
        (None, 50.0),  # no multiple (no FY1 EPS, or expected loss)
        (20.0, None),  # no growth leg (single forward year)
        (20.0, 0.0),  # flat consensus — the ratio degenerates
        (20.0, -10.0),  # expected shrinkage makes the ratio meaningless
        (0.0, 50.0),  # non-positive multiple, same guard as the trailing peg
    ],
)
def test_forward_peg_is_none_without_two_positive_legs(pe, growth):
    assert _a_valuation(pe, growth).forward_peg is None


# ───────────────────────────── GetTickerValuation ─────────────────────────────


def test_assembles_the_valuation_from_price_and_consensus():
    quotes = _FakeQuotes(price=100.0)
    estimates = _FakeEstimates(_estimates(eps_avg=5.0, eps_avg_fy2=7.5))

    out = GetTickerValuation(quotes, estimates).execute("MU")

    assert out.symbol == "MU"
    assert out.price == 100.0
    assert out.forward_pe == 20.0  # 100 / 5
    assert out.forward_eps_growth == 50.0  # 5 -> 7.5
    assert out.forward_peg == 0.4  # 20 / 50


def test_normalizes_the_symbol_before_calling_both_ports():
    quotes = _FakeQuotes()
    estimates = _FakeEstimates()

    GetTickerValuation(quotes, estimates).execute("  mu ")

    assert quotes.calls == ["MU"]  # trimmed + upper-cased once, at the edge
    assert estimates.calls == ["MU"]


def test_rejects_bad_symbols_before_touching_a_port():
    quotes = _FakeQuotes()
    estimates = _FakeEstimates()
    for bad in ("   ", "123", "TOOLONG", "BR.K"):
        with pytest.raises(ValueError):
            GetTickerValuation(quotes, estimates).execute(bad)
    assert quotes.calls == []
    assert estimates.calls == []


def test_no_stored_consensus_yields_a_null_peg_around_a_live_price():
    # A symbol the annual slice hasn't cached yet is a valid read, not an error —
    # the PEG is simply absent until its rows are filled.
    out = GetTickerValuation(_FakeQuotes(price=42.0), _FakeEstimates(_EMPTY)).execute("MU")

    assert out.price == 42.0
    assert out.forward_pe is None
    assert out.forward_eps_growth is None
    assert out.forward_peg is None


def test_single_forward_year_gives_a_multiple_but_no_peg():
    # Yahoo often estimates only one forward year: the multiple leg exists,
    # but there's no FY1->FY2 leg to divide by.
    estimates = _FakeEstimates(_estimates(eps_avg=5.0))

    out = GetTickerValuation(_FakeQuotes(price=100.0), estimates).execute("MU")

    assert out.forward_pe == 20.0
    assert out.forward_eps_growth is None
    assert out.forward_peg is None


def test_expected_loss_yields_no_peg():
    estimates = _FakeEstimates(_estimates(eps_avg=-2.0, eps_avg_fy2=1.0))

    out = GetTickerValuation(_FakeQuotes(), estimates).execute("MU")

    assert out.forward_pe is None
    assert out.forward_eps_growth is None  # growth off a non-positive base
    assert out.forward_peg is None


def test_quote_failure_propagates():
    # The price is primary — the endpoint maps this to HTTP, nothing is swallowed.
    quotes = _FakeQuotes(error=StockDataUnavailable("MU", "alpaca down"))
    with pytest.raises(StockDataUnavailable):
        GetTickerValuation(quotes, _FakeEstimates()).execute("MU")
