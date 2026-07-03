"""Tests for the ticker use case: GetTickerCard.

Offline: hand-written fakes for the quote, estimates, fundamentals, performance and
profile ports, so this exercises only the orchestration — symbol + include
normalization, assembling the card, the primary-vs-enrichment split (quote and a
*requested* consensus read propagate; the rest never sinks the card), and the
pay-per-use rule (an unrequested block costs no provider call) — plus the entity rule
the response leans on (the forward-PEG guard), independent of Alpaca, Finnhub, or the
DB.
"""

from datetime import datetime, timezone

import pytest

from app.stocks.entities import (
    AnalystEstimates,
    CompanyProfile,
    Quote,
    Stock,
    StockFundamentals,
    StockPerformance,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import (
    AnalystEstimatesProvider,
    CompanyProfileProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.ticker.entities import TickerValuation
from app.stocks.ticker.repository import StoredTickerFacts, TickerRepository
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
        self.calls: list[str] = []

    def get_profile(self, symbol: str) -> CompanyProfile:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return CompanyProfile(name="Micron Technology")


class _FakeStocks(StockDataProvider):
    """The full-snapshot source the exchange lazy fill reads on a miss."""

    def __init__(self, exchange: str | None = "NASDAQ", error=None) -> None:
        self._exchange = exchange
        self._error = error
        self.calls: list[str] = []

    def get_stock(self, symbol: str) -> Stock:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return Stock(
            symbol=symbol,
            name=None,
            exchange=self._exchange,
            price=100.0,
            open=None,
            high=None,
            low=None,
            previous_close=None,
            volume=None,
            bid=None,
            ask=None,
            as_of=None,
        )


class _FakeRepo(TickerRepository):
    """In-memory anchor-facts store; records saves so tests can assert the fills."""

    def __init__(self, name: str | None = None, exchange: str | None = None) -> None:
        self._name = name
        self._exchange = exchange
        self.name_saves: list[tuple[str, str]] = []
        self.exchange_saves: list[tuple[str, str]] = []

    def get_facts(self, symbol: str) -> StoredTickerFacts:
        return StoredTickerFacts(name=self._name, exchange=self._exchange)

    def save_name(self, symbol: str, name: str) -> None:
        self.name_saves.append((symbol, name))
        self._name = name

    def save_exchange(self, symbol: str, exchange: str) -> None:
        self.exchange_saves.append((symbol, exchange))
        self._exchange = exchange


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
    assert card.name == "Micron Technology"
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
    assert card.name == "Micron Technology"
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

    assert card.name is None
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

    assert card.name is None  # swallowed, not raised
    assert card.fundamentals is None
    assert card.performance is None


# ──────────────────────── the name + exchange lazy fills ────────────────────────


def test_stored_facts_are_served_without_vendor_calls():
    stocks = _FakeStocks()
    profile = _FakeProfile()
    repo = _FakeRepo(name="Micron Technology", exchange="NASDAQ")

    card = GetTickerCard(
        _FakeQuotes(), _FakeEstimates(), profile=profile, stocks=stocks, repository=repo
    ).execute("MU")

    assert card.name == "Micron Technology"
    assert card.exchange == "NASDAQ"
    assert profile.calls == []  # stored -> the profile vendor is never called
    assert stocks.calls == []  # stored -> the full snapshot is never fetched
    assert repo.name_saves == []
    assert repo.exchange_saves == []


def test_facts_miss_fetches_once_and_stores():
    stocks = _FakeStocks(exchange="NASDAQ")
    profile = _FakeProfile()
    repo = _FakeRepo()

    card = GetTickerCard(
        _FakeQuotes(), _FakeEstimates(), profile=profile, stocks=stocks, repository=repo
    ).execute("MU")

    assert card.name == "Micron Technology"
    assert card.exchange == "NASDAQ"
    assert profile.calls == ["MU"]  # one profile call to learn the name
    assert stocks.calls == ["MU"]  # one full-snapshot call to learn the exchange
    assert repo.name_saves == [("MU", "Micron Technology")]
    assert repo.exchange_saves == [("MU", "NASDAQ")]  # ...then both live on the row


def test_fact_fetch_failures_never_sink_the_card():
    stocks = _FakeStocks(error=StockDataUnavailable("MU", "alpaca down"))
    profile = _FakeProfile(error=StockDataUnavailable("MU", "finnhub down"))
    repo = _FakeRepo()

    card = GetTickerCard(
        _FakeQuotes(), _FakeEstimates(), profile=profile, stocks=stocks, repository=repo
    ).execute("MU")

    assert card.name is None  # swallowed, not raised
    assert card.exchange is None
    assert repo.name_saves == []
    assert repo.exchange_saves == []


def test_facts_unknown_at_the_vendors_are_not_stored():
    stocks = _FakeStocks(exchange=None)
    repo = _FakeRepo()

    card = GetTickerCard(
        _FakeQuotes(), _FakeEstimates(), stocks=stocks, repository=repo
    ).execute("MU")

    assert card.name is None  # no profile provider wired
    assert card.exchange is None  # the feed didn't know it either
    assert repo.name_saves == []  # nothing learned -> nothing written
    assert repo.exchange_saves == []


def test_facts_absent_without_wiring():
    # No repository (or vendors): the card still serves, the facts simply null —
    # except the name, which still falls through to the profile vendor per request
    # when only the repository is missing.
    card = GetTickerCard(_FakeQuotes(), _FakeEstimates()).execute("MU")
    assert card.name is None
    assert card.exchange is None


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
