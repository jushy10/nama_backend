"""Tests for the ticker use case: GetTickerCard.

Offline: hand-written fakes for the quote, estimates, fundamentals, performance,
profile and option-chain ports, so this exercises only the orchestration — symbol +
include normalization, assembling the card, the primary-vs-enrichment split (quote
and a *requested* consensus read propagate; the rest never sinks the card), and the
pay-per-use rule (an unrequested block costs no provider call) — plus the entity
rules the response leans on (the forward-PEG guard; the options-chain derivations),
independent of Alpaca, Finnhub, Yahoo, or the DB.
"""

from datetime import date, datetime, timezone

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
from app.stocks.ticker.entities import (
    OptionContract,
    TickerOptionsMetrics,
    TickerValuation,
)
from app.stocks.ticker.ports import OptionChainProvider
from app.stocks.ticker.use_cases import GetTickerCard

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
        self.calls: list[str] = []

    def get_fundamentals(self, symbol: str) -> StockFundamentals:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return _fundamentals()


class _FakePerformance(StockPerformanceProvider):
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[str] = []

    def get_performance(self, symbol: str) -> StockPerformance:
        self.calls.append(symbol)
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


_TODAY = date(2026, 7, 3)
_NEAR = date(2026, 7, 31)  # ~28 days out — the ~1-month window's pick
_FAR = date(2026, 10, 2)  # ~91 days out — the ~3-month window's pick


def _call(expiration, strike, *, bid=None, ask=None, last=None, volume=None, iv=None):
    return OptionContract(
        expiration=expiration, strike=strike, is_call=True,
        bid=bid, ask=ask, last_price=last, volume=volume, implied_volatility=iv,
    )


def _put(expiration, strike, *, bid=None, ask=None, last=None, volume=None, iv=None):
    return OptionContract(
        expiration=expiration, strike=strike, is_call=False,
        bid=bid, ask=ask, last_price=last, volume=volume, implied_volatility=iv,
    )


def _near_chain() -> tuple[OptionContract, ...]:
    # A liquid ATM pair around a 100.0 spot: straddle mid = 3.0 + 2.0 = 5.0.
    return (
        _call(_NEAR, 100.0, bid=2.8, ask=3.2, volume=500, iv=0.25),
        _put(_NEAR, 100.0, bid=1.9, ask=2.1, volume=1000, iv=0.27),
        _call(_NEAR, 110.0, bid=0.4, ask=0.6, volume=200, iv=0.30),
    )


def _far_chain() -> tuple[OptionContract, ...]:
    return (
        _put(_FAR, 100.0, bid=3.9, ask=4.1, volume=200, iv=0.24),
        _call(_FAR, 100.0, bid=5.9, ask=6.1, volume=300, iv=0.23),
    )


class _FakeOptions(OptionChainProvider):
    def __init__(self, expirations=(), chains=None, error=None) -> None:
        self._expirations = expirations
        self._chains = chains or {}
        self._error = error
        self.calls: list[tuple] = []

    def get_expirations(self, symbol: str) -> tuple[date, ...]:
        self.calls.append(("expirations", symbol))
        if self._error is not None:
            raise self._error
        return tuple(self._expirations)

    def get_chain(self, symbol: str, expiration: date) -> tuple[OptionContract, ...]:
        self.calls.append(("chain", symbol, expiration))
        if self._error is not None:
            raise self._error
        return tuple(self._chains.get(expiration, ()))


def _options_provider() -> _FakeOptions:
    return _FakeOptions(
        expirations=(date(2026, 7, 10), _NEAR, _FAR, date(2027, 1, 15)),
        chains={_NEAR: _near_chain(), _FAR: _far_chain()},
    )


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


def test_options_metrics_derives_all_four_reads_from_the_two_chains():
    m = TickerOptionsMetrics.from_chains(100.0, _near_chain(), _far_chain())
    # ATM IV averages the call/put nearest the money (0.25 + 0.27, NOT the
    # further-out 110 call's 0.30), reported as a percent.
    assert m.implied_volatility == pytest.approx(26.0)
    # Expected move is the ATM straddle over spot: (3.0 + 2.0) / 100.
    assert m.expected_move_percent == pytest.approx(5.0)
    assert m.expected_move_by == _NEAR
    # Insurance is the far ATM put's mid over spot: 4.0 / 100.
    assert m.insurance_cost_percent == pytest.approx(4.0)
    assert m.insurance_expires == _FAR
    # Put/call pools both sampled expiries: (1000 + 200) / (500 + 200 + 300).
    assert m.put_call_ratio == pytest.approx(1.2)


def test_options_metrics_does_not_double_count_a_shared_expiry():
    # Sparse listings can land both windows on the same expiry; its volume
    # must be pooled once.
    m = TickerOptionsMetrics.from_chains(100.0, _near_chain(), _near_chain())
    assert m.put_call_ratio == pytest.approx(1000 / 700)
    assert m.insurance_expires == _NEAR  # the put still prices off the shared chain
    assert m.insurance_cost_percent == pytest.approx(2.0)


def test_options_metrics_fills_what_it_can_from_a_one_sided_chain():
    # Only the insurance expiry has contracts: no IV/straddle, but the put and
    # the (far-only) volume pool still serve.
    m = TickerOptionsMetrics.from_chains(100.0, (), _far_chain())
    assert m.implied_volatility is None
    assert m.expected_move_percent is None
    assert m.expected_move_by is None
    assert m.insurance_cost_percent == pytest.approx(4.0)
    assert m.insurance_expires == _FAR
    assert m.put_call_ratio == pytest.approx(200 / 300)


def test_options_metrics_treats_dead_quotes_as_unpriceable():
    # Zero bid/ask with no last trade is a dead quote, not a free straddle —
    # and with no priceable volume-carrying calls the ratio degenerates too.
    chain = (
        _call(_NEAR, 100.0, bid=0.0, ask=0.0, iv=0.25),
        _put(_NEAR, 100.0, bid=0.0, ask=0.0, iv=0.27),
    )
    m = TickerOptionsMetrics.from_chains(100.0, chain, chain)
    assert m.expected_move_percent is None
    assert m.insurance_cost_percent is None
    assert m.implied_volatility == pytest.approx(26.0)  # IV is quoted, not priced
    assert m.put_call_ratio is None  # no call volume to divide by


def test_options_metrics_mid_falls_back_to_the_last_trade():
    chain = (
        _call(_NEAR, 100.0, last=3.0, volume=1),
        _put(_NEAR, 100.0, last=2.0, volume=1),
    )
    m = TickerOptionsMetrics.from_chains(100.0, chain, ())
    assert m.expected_move_percent == pytest.approx(5.0)


def test_options_metrics_is_empty_at_a_non_positive_price():
    # Every figure is a ratio to spot; a broken quote can't anchor any of them.
    m = TickerOptionsMetrics.from_chains(0.0, _near_chain(), _far_chain())
    assert m == TickerOptionsMetrics(None, None, None, None, None, None)


# ───────────────────────────── GetTickerCard ─────────────────────────────


def test_assembles_the_full_card_when_everything_is_included():
    quotes = _FakeQuotes(price=100.0)
    estimates = _FakeEstimates(_estimates(eps_avg=5.0, eps_avg_fy2=7.5))

    card = GetTickerCard(
        quotes, estimates, _FakeFundamentals(), _FakePerformance(), _FakeProfile()
    ).execute("MU", include=["dividend", "performance", "metrics"])

    assert card.quote.symbol == "MU"
    assert card.quote.price == 100.0
    assert card.include == {"dividend", "performance", "metrics"}
    assert card.valuation.forward_pe == 20.0  # 100 / 5
    assert card.valuation.forward_eps_growth == 50.0  # 5 -> 7.5
    assert card.valuation.forward_peg == 0.4  # 20 / 50
    assert card.profile == CompanyProfile(name="Micron Technology")
    assert card.fundamentals == _fundamentals()
    assert card.performance == _performance()


def test_unrequested_blocks_cost_no_provider_call():
    # Pay-per-use: without includes, neither the consensus read nor the
    # performance windows are fetched — the card is just quote + name + cap.
    estimates = _FakeEstimates(_estimates(eps_avg=5.0, eps_avg_fy2=7.5))
    performance = _FakePerformance()

    card = GetTickerCard(
        _FakeQuotes(), estimates, _FakeFundamentals(), performance, _FakeProfile()
    ).execute("MU")

    assert estimates.calls == []  # never touched
    assert performance.calls == []  # never touched
    assert card.include == frozenset()
    assert card.valuation is None
    assert card.performance is None
    # The always-on parts still ride along.
    assert card.profile is not None
    assert card.fundamentals is not None


def test_includes_accept_comma_separated_and_mixed_case_values():
    estimates = _FakeEstimates(_estimates(eps_avg=5.0))
    performance = _FakePerformance()

    card = GetTickerCard(
        _FakeQuotes(), estimates, _FakeFundamentals(), performance
    ).execute("MU", include=["Dividend, METRICS"])

    assert card.include == {"dividend", "metrics"}
    assert estimates.calls == ["MU"]  # metrics requested -> consensus fetched
    assert performance.calls == []  # performance not requested


def test_unknown_include_is_rejected_before_touching_a_port():
    quotes = _FakeQuotes()
    estimates = _FakeEstimates()

    with pytest.raises(ValueError, match="Unknown include"):
        GetTickerCard(quotes, estimates).execute("MU", include=["earnings"])

    assert quotes.calls == []  # rejected at the edge, like a bad symbol
    assert estimates.calls == []


def test_normalizes_the_symbol_before_calling_the_ports():
    quotes = _FakeQuotes()
    estimates = _FakeEstimates()

    GetTickerCard(quotes, estimates).execute("  mu ", include=["metrics"])

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
    card = GetTickerCard(_FakeQuotes(price=42.0), _FakeEstimates(_EMPTY)).execute(
        "MU", include=["metrics"]
    )

    assert card.quote.price == 42.0
    assert card.valuation.forward_peg is None


def test_single_forward_year_gives_a_multiple_but_no_peg():
    # Yahoo often estimates only one forward year: the multiple leg exists,
    # but there's no FY1->FY2 leg to divide by.
    estimates = _FakeEstimates(_estimates(eps_avg=5.0))

    card = GetTickerCard(_FakeQuotes(price=100.0), estimates).execute(
        "MU", include=["metrics"]
    )

    assert card.valuation.forward_pe == 20.0
    assert card.valuation.forward_eps_growth is None
    assert card.valuation.forward_peg is None


def test_expected_loss_yields_no_peg():
    estimates = _FakeEstimates(_estimates(eps_avg=-2.0, eps_avg_fy2=1.0))

    card = GetTickerCard(_FakeQuotes(), estimates).execute("MU", include=["metrics"])

    assert card.valuation.forward_pe is None
    assert card.valuation.forward_eps_growth is None  # growth off a non-positive base
    assert card.valuation.forward_peg is None


def test_unwired_enrichment_leaves_the_blocks_none():
    # No fundamentals/performance/profile provider (e.g. no FINNHUB_API_KEY): the
    # card still serves, its enrichment blocks simply absent even when requested.
    card = GetTickerCard(_FakeQuotes(), _FakeEstimates()).execute(
        "MU", include=["dividend", "performance"]
    )

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
    ).execute("MU", include=["dividend", "performance"])

    assert card.profile is None  # swallowed, not raised
    assert card.fundamentals is None
    assert card.performance is None


def test_quote_failure_propagates():
    # The quote is primary — the endpoint maps this to HTTP, nothing is swallowed.
    quotes = _FakeQuotes(error=StockDataUnavailable("MU", "alpaca down"))
    with pytest.raises(StockDataUnavailable):
        GetTickerCard(quotes, _FakeEstimates()).execute("MU")


def test_estimates_failure_propagates_when_metrics_is_requested():
    # The consensus read is primary when asked for: the metrics block exists to
    # price the forward PEG, so it degrades loudly rather than silently.
    estimates = _FakeEstimates(error=StockDataUnavailable("MU", "db down"))
    with pytest.raises(StockDataUnavailable):
        GetTickerCard(_FakeQuotes(), estimates).execute("MU", include=["metrics"])


# ──────────────────────── the options_metrics block ────────────────────────


def _card_with_options(options: _FakeOptions, include=("options_metrics",)):
    return GetTickerCard(
        _FakeQuotes(price=100.0),
        _FakeEstimates(),
        options=options,
        today=lambda: _TODAY,
    ).execute("MU", include=list(include))


def test_options_metrics_samples_the_month_and_quarter_expiries():
    options = _options_provider()

    card = _card_with_options(options)

    # Nearest listed expiry to each window wins: ~1 month → Jul 31, ~3 → Oct 2
    # (not the Jul 10 weekly or the Jan LEAP).
    assert ("chain", "MU", _NEAR) in options.calls
    assert ("chain", "MU", _FAR) in options.calls
    m = card.options_metrics
    assert m.implied_volatility == pytest.approx(26.0)
    assert m.expected_move_percent == pytest.approx(5.0)
    assert m.expected_move_by == _NEAR
    assert m.insurance_cost_percent == pytest.approx(4.0)
    assert m.insurance_expires == _FAR
    assert m.put_call_ratio == pytest.approx(1.2)


def test_options_metrics_fetches_a_shared_expiry_once():
    # Only one listed expiry: both windows land on it, and the chain is
    # fetched a single time (the entity dedupes its volume too).
    options = _FakeOptions(expirations=(_NEAR,), chains={_NEAR: _near_chain()})

    card = _card_with_options(options)

    assert [c for c in options.calls if c[0] == "chain"] == [("chain", "MU", _NEAR)]
    assert card.options_metrics.put_call_ratio == pytest.approx(1000 / 700)


def test_unrequested_options_metrics_cost_no_provider_call():
    options = _options_provider()

    card = _card_with_options(options, include=())

    assert options.calls == []  # pay-per-use, like the other opt-ins
    assert card.options_metrics is None


def test_no_listed_options_is_no_coverage_not_an_error():
    # Expirations empty (or all in the past): a valid read with the block absent.
    options = _FakeOptions(expirations=(date(2026, 6, 19),))

    card = _card_with_options(options)

    assert card.options_metrics is None
    assert [c for c in options.calls if c[0] == "chain"] == []  # nothing to fetch


def test_options_failure_never_sinks_the_card():
    # Best-effort even when requested: the options read is a live Yahoo call and
    # a blocked IP must not take the quote down.
    options = _FakeOptions(error=StockDataUnavailable("MU", "yahoo blocked"))

    card = _card_with_options(options)

    assert card.quote.price == 100.0
    assert card.options_metrics is None


def test_unwired_options_provider_leaves_the_block_none():
    card = GetTickerCard(_FakeQuotes(), _FakeEstimates()).execute(
        "MU", include=["options_metrics"]
    )
    assert card.options_metrics is None
