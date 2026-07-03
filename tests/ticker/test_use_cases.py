"""Tests for the ticker use case: GetTickerCard.

Offline: hand-written fakes for the quote, estimates, fundamentals and performance
ports, so this exercises only the orchestration — symbol normalization, assembling the
card from the live quote + stored consensus, the primary-vs-enrichment split (quote and
estimates propagate; fundamentals and performance never sink the card) — plus the
entity rule the response leans on (the forward-PEG guard), independent of Alpaca,
Finnhub, or the DB.
"""

from datetime import datetime, timezone

import pytest

from app.stocks.entities import (
    AnalystEstimates,
    CompanyProfile,
    Quote,
    StockFundamentals,
    StockPerformance,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import (
    AnalystEstimatesProvider,
    CompanyProfileProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.ticker.entities import TickerValuation
from app.stocks.ticker.use_cases import GetTickerCard

_EMPTY = AnalystEstimates(
    fiscal_year=None, period_end=None, eps_avg=None, revenue_avg=None
)


def _a_quote(symbol: str, price: float, previous_close: float | None = None) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        previous_close=previous_close,
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


def _fundamentals() -> StockFundamentals:
    return StockFundamentals(
        market_cap=1_000_000_000.0, dividend_per_share=0.46, dividend_yield=0.05
    )


def _performance() -> StockPerformance:
    return StockPerformance(
        one_week=1.0, one_month=2.0, three_month=3.0, six_month=4.0, ytd=5.0, one_year=6.0
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
    def __init__(self, estimates: AnalystEstimates = _EMPTY, error=None) -> None:
        self._estimates = estimates
        self._error = error
        self.calls: list[str] = []

    def get_estimates(self, symbol: str) -> AnalystEstimates:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._estimates


class _FakeFundamentals(StockFundamentalsProvider):
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def get_fundamentals(self, symbol: str) -> StockFundamentals:
        if self._error is not None:
            raise self._error
        return _fundamentals()


class _FakePerformance(StockPerformanceProvider):
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def get_performance(self, symbol: str) -> StockPerformance:
        if self._error is not None:
            raise self._error
        return _performance()


class _FakeProfile(CompanyProfileProvider):
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def get_profile(self, symbol: str) -> CompanyProfile:
        if self._error is not None:
            raise self._error
        return CompanyProfile(name="Micron Technology")


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


# ───────────────────────────── GetTickerCard ─────────────────────────────


def test_assembles_the_card_from_all_the_ports():
    quotes = _FakeQuotes(price=100.0)
    estimates = _FakeEstimates(_estimates(eps_avg=5.0, eps_avg_fy2=7.5))

    card = GetTickerCard(
        quotes, estimates, _FakeFundamentals(), _FakePerformance(), _FakeProfile()
    ).execute("MU")

    assert card.quote.symbol == "MU"
    assert card.quote.price == 100.0
    assert card.valuation.forward_pe == 20.0  # 100 / 5
    assert card.valuation.forward_eps_growth == 50.0  # 5 -> 7.5
    assert card.valuation.forward_peg == 0.4  # 20 / 50
    assert card.profile == CompanyProfile(name="Micron Technology")
    assert card.fundamentals == _fundamentals()
    assert card.performance == _performance()


def test_normalizes_the_symbol_before_calling_the_ports():
    quotes = _FakeQuotes()
    estimates = _FakeEstimates()

    GetTickerCard(quotes, estimates).execute("  mu ")

    assert quotes.calls == ["MU"]  # trimmed + upper-cased once, at the edge
    assert estimates.calls == ["MU"]


def test_rejects_bad_symbols_before_touching_a_port():
    quotes = _FakeQuotes()
    estimates = _FakeEstimates()
    for bad in ("   ", "123", "TOOLONG", "BR.K"):
        with pytest.raises(ValueError):
            GetTickerCard(quotes, estimates).execute(bad)
    assert quotes.calls == []
    assert estimates.calls == []


def test_no_stored_consensus_yields_a_null_peg_around_a_live_quote():
    # A symbol the annual slice hasn't cached yet is a valid read, not an error —
    # the PEG is simply absent until its rows are filled.
    card = GetTickerCard(_FakeQuotes(price=42.0), _FakeEstimates(_EMPTY)).execute("MU")

    assert card.quote.price == 42.0
    assert card.valuation.forward_peg is None


def test_single_forward_year_gives_a_multiple_but_no_peg():
    # Yahoo often estimates only one forward year: the multiple leg exists,
    # but there's no FY1->FY2 leg to divide by.
    estimates = _FakeEstimates(_estimates(eps_avg=5.0))

    card = GetTickerCard(_FakeQuotes(price=100.0), estimates).execute("MU")

    assert card.valuation.forward_pe == 20.0
    assert card.valuation.forward_eps_growth is None
    assert card.valuation.forward_peg is None


def test_expected_loss_yields_no_peg():
    estimates = _FakeEstimates(_estimates(eps_avg=-2.0, eps_avg_fy2=1.0))

    card = GetTickerCard(_FakeQuotes(), estimates).execute("MU")

    assert card.valuation.forward_pe is None
    assert card.valuation.forward_eps_growth is None  # growth off a non-positive base
    assert card.valuation.forward_peg is None


def test_unwired_enrichment_leaves_the_blocks_none():
    # No fundamentals/performance/profile provider (e.g. no FINNHUB_API_KEY): the
    # card still serves, its enrichment blocks simply absent.
    card = GetTickerCard(_FakeQuotes(), _FakeEstimates()).execute("MU")

    assert card.profile is None
    assert card.fundamentals is None
    assert card.performance is None


@pytest.mark.parametrize(
    "error",
    [StockNotFound("MU"), StockDataUnavailable("MU", "finnhub down")],
)
def test_enrichment_failures_never_sink_the_card(error):
    card = GetTickerCard(
        _FakeQuotes(),
        _FakeEstimates(),
        _FakeFundamentals(error=error),
        _FakePerformance(error=error),
        _FakeProfile(error=error),
    ).execute("MU")

    assert card.profile is None  # swallowed, not raised
    assert card.fundamentals is None
    assert card.performance is None


def test_quote_failure_propagates():
    # The quote is primary — the endpoint maps this to HTTP, nothing is swallowed.
    quotes = _FakeQuotes(error=StockDataUnavailable("MU", "alpaca down"))
    with pytest.raises(StockDataUnavailable):
        GetTickerCard(quotes, _FakeEstimates()).execute("MU")


def test_estimates_failure_propagates():
    # The consensus read is primary too: the card exists to price the forward PEG.
    estimates = _FakeEstimates(error=StockDataUnavailable("MU", "db down"))
    with pytest.raises(StockDataUnavailable):
        GetTickerCard(_FakeQuotes(), estimates).execute("MU")
