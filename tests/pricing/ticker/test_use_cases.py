from datetime import date, datetime, timezone

import pytest

from app.domains.financials.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.domains.financials.earnings.quarterly.interfaces import QuarterlyEarningsAdapter
from app.domains.shared.entities import (
    Quote,
    Stock,
    StockPerformance,
)
from app.domains.shared.interfaces import (
    StockDataAdapter,
    StockPerformanceAdapter,
    StockQuoteAdapter,
)
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.etfs.interfaces import EtfLookupRepositoryAdapter
from app.domains.pricing.ticker.entities import (
    OptionContract,
    TickerOptionsMetrics,
    TickerValuation,
)
from app.domains.pricing.ticker.entities import (
    ASSET_TYPE_EQUITY,
    ASSET_TYPE_ETF,
    TickerClassification,
)
from app.domains.pricing.ticker.interfaces import OptionChainAdapter
from app.domains.pricing.ticker.repository import StoredTickerFacts, TickerRepository
from app.domains.pricing.ticker.use_cases import ClassifyTicker, GetTickerCard


def _a_quote(symbol: str, price: float) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        previous_close=None,
        bid=None,
        ask=None,
        as_of=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )


def _performance() -> StockPerformance:
    return StockPerformance(
        one_week=1.0, one_month=2.0, three_month=3.0, six_month=4.0, ytd=5.0, one_year=6.0
    )


class _FakeQuotes(StockQuoteAdapter):
    def __init__(self, price: float = 100.0, error: Exception | None = None) -> None:
        self._price = price
        self._error = error
        self.calls: list[str] = []

    def get_quote(self, symbol: str) -> Quote:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return _a_quote(symbol, self._price)


class _FakePerformance(StockPerformanceAdapter):
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[str] = []

    def get_performance(self, symbol: str) -> StockPerformance:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return _performance()


class _FakeStocks(StockDataAdapter):
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
    def __init__(
        self,
        name: str | None = None,
        exchange: str | None = None,
        *,
        market_cap: float | None = None,
        sector: str | None = None,
        industry: str | None = None,
        revenue_growth_yoy: float | None = None,
        eps_growth_yoy: float | None = None,
        fcf_per_share: float | None = None,
        ocf_per_share: float | None = None,
        fcf_growth_yoy: float | None = None,
        gross_margin: float | None = None,
        operating_margin: float | None = None,
        net_margin: float | None = None,
        dividend_per_share: float | None = None,
        ebitda: float | None = None,
        total_debt: float | None = None,
        cash_and_equivalents: float | None = None,
        shares_outstanding: float | None = None,
    ) -> None:
        self._name = name
        self._exchange = exchange
        self._market_cap = market_cap
        self._sector = sector
        self._industry = industry
        self._revenue_growth_yoy = revenue_growth_yoy
        self._eps_growth_yoy = eps_growth_yoy
        self._fcf_per_share = fcf_per_share
        self._ocf_per_share = ocf_per_share
        self._fcf_growth_yoy = fcf_growth_yoy
        self._gross_margin = gross_margin
        self._operating_margin = operating_margin
        self._net_margin = net_margin
        self._dividend_per_share = dividend_per_share
        self._ebitda = ebitda
        self._total_debt = total_debt
        self._cash_and_equivalents = cash_and_equivalents
        self._shares_outstanding = shares_outstanding
        self.name_saves: list[tuple[str, str]] = []
        self.exchange_saves: list[tuple[str, str]] = []

    def get_facts(self, symbol: str) -> StoredTickerFacts:
        return StoredTickerFacts(
            name=self._name,
            exchange=self._exchange,
            market_cap=self._market_cap,
            sector=self._sector,
            industry=self._industry,
            revenue_growth_yoy=self._revenue_growth_yoy,
            eps_growth_yoy=self._eps_growth_yoy,
            fcf_per_share=self._fcf_per_share,
            ocf_per_share=self._ocf_per_share,
            fcf_growth_yoy=self._fcf_growth_yoy,
            gross_margin=self._gross_margin,
            operating_margin=self._operating_margin,
            net_margin=self._net_margin,
            dividend_per_share=self._dividend_per_share,
            ebitda=self._ebitda,
            total_debt=self._total_debt,
            cash_and_equivalents=self._cash_and_equivalents,
            shares_outstanding=self._shares_outstanding,
        )

    def save_name(self, symbol: str, name: str) -> None:
        self.name_saves.append((symbol, name))
        self._name = name

    def save_exchange(self, symbol: str, exchange: str) -> None:
        self.exchange_saves.append((symbol, exchange))
        self._exchange = exchange


class _FakeEtfs(EtfLookupRepositoryAdapter):
    def __init__(self, is_member: bool = False) -> None:
        self._is_member = is_member
        self.calls: list[str] = []

    def is_etf(self, ticker: str) -> bool:
        self.calls.append(ticker)
        return self._is_member

    def get(self, ticker: str):
        return None  # unused by the card (it only asks is_etf)

    def get_stored_profile(self, ticker: str):
        from app.domains.etfs.entities import EtfProfile

        return EtfProfile.empty()  # unused by the card (it only asks is_etf)


def _a_reported_quarter(year: int, quarter: int, eps: float) -> QuarterlyEarnings:
    return QuarterlyEarnings(
        fiscal_year=year, fiscal_quarter=quarter, period_end=None, report_date=None,
        eps_actual=eps, eps_estimate=None, eps_surprise=None,
        eps_surprise_percent=None, revenue_estimate=None,
    )


def _four_quarters(*eps: float) -> QuarterlyEarningsTimeline:
    quarters = tuple(
        _a_reported_quarter(2026, i + 1, e) for i, e in enumerate(eps)
    )
    return QuarterlyEarningsTimeline(symbol="MU", quarters=quarters)


class _FakeEarnings(QuarterlyEarningsAdapter):
    def __init__(self, timeline: QuarterlyEarningsTimeline | None = None, error=None):
        self._timeline = timeline or QuarterlyEarningsTimeline("MU", ())
        self._error = error
        self.calls: list[str] = []

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._timeline


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


class _FakeOptions(OptionChainAdapter):
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


def test_trailing_pe_divides_price_by_the_consensus_ttm():
    v = TickerValuation(symbol="MU", price=100.0, ttm_eps=8.0)
    assert v.trailing_pe == 12.5


@pytest.mark.parametrize("ttm", [None, 0.0, -3.2])
def test_trailing_pe_is_none_without_a_positive_ttm(ttm):
    # No cached quarters (or a loss-making trailing year): a non-positive TTM has
    # no meaningful P/E.
    v = TickerValuation(symbol="MU", price=100.0, ttm_eps=ttm)
    assert v.trailing_pe is None


def test_fcf_multiples_price_the_trailing_fcf_per_share():
    # $5 FCF/share against a $100 quote: a 20x P/FCF and a 5% FCF yield (its
    # reciprocal). Both taken at the card's live price, like the P/E.
    v = TickerValuation(symbol="MU", price=100.0, fcf_per_share=5.0)
    assert v.price_to_fcf == 20.0
    assert v.fcf_yield == 5.0


@pytest.mark.parametrize("fcf", [None, 0.0, -2.0])
def test_price_to_fcf_is_none_without_a_positive_fcf(fcf):
    # A non-positive FCF (a cash-burner) has no meaningful multiple — the same
    # guard trailing_pe applies to a loss-making year.
    v = TickerValuation(symbol="MU", price=100.0, fcf_per_share=fcf)
    assert v.price_to_fcf is None


def test_fcf_yield_keeps_its_sign_for_a_cash_burner():
    # Unlike the multiple, a negative yield is informative — it says the company
    # has negative free cash flow, so it's served (and P/FCF stays null).
    v = TickerValuation(symbol="MU", price=100.0, fcf_per_share=-2.0)
    assert v.fcf_yield == -2.0
    assert v.price_to_fcf is None


def test_fcf_multiples_are_none_without_the_fcf_leg():
    # No fundamentals coverage: both figures are simply absent around a live price.
    v = TickerValuation(symbol="MU", price=100.0)
    assert v.price_to_fcf is None
    assert v.fcf_yield is None


def test_enterprise_value_and_ev_ebitda_price_shares_debt_cash_live():
    # $100 quote x 1B shares = $100B market cap; + $20B debt - $5B cash = $115B EV;
    # over $10B EBITDA -> 11.5x. All taken at the live price, like the P/E.
    v = TickerValuation(
        symbol="MU",
        price=100.0,
        shares_outstanding=1_000_000_000.0,
        total_debt=20_000_000_000.0,
        cash_and_equivalents=5_000_000_000.0,
        ebitda=10_000_000_000.0,
    )
    assert v.enterprise_value == 115_000_000_000.0
    assert v.ev_to_ebitda == 11.5


def test_enterprise_value_defaults_missing_debt_and_cash_to_zero():
    # A debt-free, cash-light name Yahoo carries no debt/cash for: its EV is just its
    # live market cap (the legs default to 0 rather than nulling the whole figure).
    v = TickerValuation(symbol="MU", price=50.0, shares_outstanding=1_000_000_000.0)
    assert v.enterprise_value == 50_000_000_000.0


@pytest.mark.parametrize("shares", [None, 0.0, -1.0])
def test_enterprise_value_is_none_without_a_positive_share_count(shares):
    # No usable share count -> no market cap -> no enterprise value (and so no EV/EBITDA).
    v = TickerValuation(
        symbol="MU", price=100.0, shares_outstanding=shares, ebitda=10_000_000_000.0
    )
    assert v.enterprise_value is None
    assert v.ev_to_ebitda is None


@pytest.mark.parametrize("ebitda", [None, 0.0, -3_000_000_000.0])
def test_ev_ebitda_is_none_without_a_positive_ebitda(ebitda):
    # EV/EBITDA off a non-positive EBITDA is meaningless — the same guard trailing_pe
    # applies to a loss. Enterprise value itself still resolves.
    v = TickerValuation(
        symbol="MU", price=100.0, shares_outstanding=1_000_000_000.0, ebitda=ebitda
    )
    assert v.enterprise_value == 100_000_000_000.0
    assert v.ev_to_ebitda is None


def test_ev_ebitda_keeps_a_negative_enterprise_value():
    # A net-cash company worth less than its cash: $10B market cap - $50B net cash = -$40B
    # EV; over $10B EBITDA -> -4.0. The negative multiple is a real "valued below net cash"
    # reading, so it's served (positive EBITDA is all that's required).
    v = TickerValuation(
        symbol="MU",
        price=10.0,
        shares_outstanding=1_000_000_000.0,
        cash_and_equivalents=50_000_000_000.0,
        ebitda=10_000_000_000.0,
    )
    assert v.enterprise_value == -40_000_000_000.0
    assert v.ev_to_ebitda == -4.0


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


def test_assembles_the_full_card_when_everything_is_included():
    quotes = _FakeQuotes(price=100.0)
    repo = _FakeRepo(
        name="Micron Technology",
        gross_margin=52.1,
        operating_margin=38.9,
        net_margin=33.5,
        dividend_per_share=0.46,
    )

    card = GetTickerCard(
        quotes, _FakePerformance(), repository=repo
    ).run("MU", include=["dividend", "performance", "metrics"])

    assert card.quote.symbol == "MU"
    assert card.quote.price == 100.0
    assert card.include == {"dividend", "performance", "metrics"}
    assert card.valuation is not None  # the metrics block was requested
    assert card.name == "Micron Technology"  # served off the anchor
    # The margins + dividend per share ride the same anchor read (no live vendor).
    assert card.gross_margin == 52.1
    assert card.operating_margin == 38.9
    assert card.net_margin == 33.5
    assert card.dividend_per_share == 0.46
    assert card.performance == _performance()


def test_unrequested_blocks_cost_no_provider_call():
    # Pay-per-use: without includes, the performance windows aren't fetched and no
    # valuation is built — market cap, margins and dividend all ride the anchor now,
    # so a bare card is just quote + name + the DB facts, no provider call.
    performance = _FakePerformance()
    repo = _FakeRepo(name="Micron Technology", dividend_per_share=0.46)

    card = GetTickerCard(
        _FakeQuotes(), performance, repository=repo
    ).run("MU")

    assert performance.calls == []  # never touched
    assert card.include == frozenset()
    assert card.valuation is None
    assert card.performance is None
    # The always-on name + anchor facts still ride along (off the DB, no provider call).
    assert card.name == "Micron Technology"
    assert card.dividend_per_share == 0.46


def test_performance_only_include_builds_no_valuation():
    # Pay-per-use per block: a performance-only card fetches the performance windows
    # but builds no metrics valuation (that's a separate opt-in).
    performance = _FakePerformance()

    card = GetTickerCard(
        _FakeQuotes(), performance
    ).run("MU", include=["performance"])

    assert performance.calls == ["MU"]
    assert card.performance is not None
    assert card.valuation is None


def test_dividend_and_margins_ride_the_anchor():
    # The dividend per share and margins are served off the anchor read — no live
    # fundamentals vendor — available on the card for the presenter to gate/price.
    repo = _FakeRepo(
        dividend_per_share=0.46,
        gross_margin=52.1,
        operating_margin=38.9,
        net_margin=33.5,
    )

    card = GetTickerCard(
        _FakeQuotes(price=100.0), repository=repo
    ).run("MU", include=["dividend", "metrics"])

    assert card.dividend_per_share == 0.46
    assert card.gross_margin == 52.1
    assert card.operating_margin == 38.9
    assert card.net_margin == 33.5


def test_stored_anchor_facts_flow_onto_the_card():
    # market cap, sector, industry and the trailing growth are read straight off
    # the anchor (the universe/annual syncs' writes) — no provider call.
    repo = _FakeRepo(
        name="Micron Technology",
        exchange="NASDAQ",
        market_cap=1.09e12,
        sector="technology",
        industry="semiconductors",
        revenue_growth_yoy=61.6,
        eps_growth_yoy=587.4,
    )

    card = GetTickerCard(
        _FakeQuotes(), repository=repo
    ).run("MU")

    assert card.market_cap == 1.09e12
    assert card.sector == "technology"
    assert card.industry == "semiconductors"
    assert card.revenue_growth_yoy == 61.6
    assert card.eps_growth_yoy == 587.4


def test_asset_type_is_etf_when_the_symbol_is_in_the_etf_universe():
    etfs = _FakeEtfs(is_member=True)

    card = GetTickerCard(_FakeQuotes(), etfs=etfs).run("VOO")

    assert card.asset_type == ASSET_TYPE_ETF
    assert etfs.calls == ["VOO"]  # a single membership check on the normalized symbol


def test_asset_type_is_equity_for_a_stock():
    etfs = _FakeEtfs(is_member=False)

    card = GetTickerCard(_FakeQuotes(), etfs=etfs).run("MU")

    assert card.asset_type == ASSET_TYPE_EQUITY


def test_classify_ticker_is_etf_for_a_fund():
    etfs = _FakeEtfs(is_member=True)

    result = ClassifyTicker(etfs).run("voo")

    assert result == TickerClassification(ticker="VOO", asset_type=ASSET_TYPE_ETF)
    # Normalizes to upper-case, then a single membership check on it — no quote.
    assert etfs.calls == ["VOO"]


def test_classify_ticker_is_equity_for_a_stock():
    etfs = _FakeEtfs(is_member=False)

    result = ClassifyTicker(etfs).run("AAPL")

    assert result == TickerClassification(ticker="AAPL", asset_type=ASSET_TYPE_EQUITY)


def test_classify_ticker_rejects_a_malformed_symbol():
    # Same normalization guard as the card: empty / non-alpha / too long is a ValueError.
    with pytest.raises(ValueError):
        ClassifyTicker(_FakeEtfs()).run("")


def test_asset_type_defaults_to_equity_without_an_etfs_lookup():
    # No etfs repository wired (a bare use case): the card still resolves a non-null asset_type.
    card = GetTickerCard(_FakeQuotes()).run("MU")
    assert card.asset_type == ASSET_TYPE_EQUITY


def test_includes_accept_comma_separated_and_mixed_case_values():
    performance = _FakePerformance()

    card = GetTickerCard(
        _FakeQuotes(), performance
    ).run("MU", include=["Dividend, METRICS"])

    assert card.include == {"dividend", "metrics"}
    assert performance.calls == []  # performance not requested


def test_unknown_include_is_rejected_before_touching_a_port():
    quotes = _FakeQuotes()

    with pytest.raises(ValueError, match="Unknown include"):
        GetTickerCard(quotes).run("MU", include=["earnings"])

    assert quotes.calls == []  # rejected at the edge, like a bad symbol


def test_normalizes_the_symbol_before_calling_the_ports():
    quotes = _FakeQuotes()

    GetTickerCard(quotes).run("  mu ", include=["metrics"])

    assert quotes.calls == ["MU"]  # trimmed + upper-cased once, at the edge


def test_accepts_a_canadian_symbol_and_preserves_its_venue_suffix():
    # A Canadian listing (Yahoo suffix) must pass the guard and reach the port with its
    # suffix intact — that suffix is what the price router dispatches on (US -> Alpaca /
    # CA -> Yahoo), so stripping or rejecting it would break every CA card and chart.
    quotes = _FakeQuotes()

    for raw, expected in ((" shop.to ", "SHOP.TO"), ("cp.to", "CP.TO"), ("x.ne", "X.NE")):
        quotes.calls.clear()
        GetTickerCard(quotes).run(raw)
        assert quotes.calls == [expected]


def test_rejects_bad_symbols_before_touching_a_port():
    quotes = _FakeQuotes()
    for bad in ("   ", "123", "TOOLONG", "BR.K"):
        with pytest.raises(ValueError):
            GetTickerCard(quotes).run(bad)
    assert quotes.calls == []


def test_unwired_enrichment_leaves_the_blocks_none():
    # No performance provider and no anchor repository (a bare use case): the card
    # still serves, its enrichment blocks simply absent even when requested.
    card = GetTickerCard(_FakeQuotes()).run(
        "MU", include=["dividend", "performance"]
    )

    assert card.name is None
    assert card.dividend_per_share is None
    assert card.performance is None


@pytest.mark.parametrize(
    "error",
    [StockNotFound("MU"), StockDataUnavailable("MU", "alpaca down")],
)
def test_enrichment_failures_never_sink_the_card(error):
    # The performance read is best-effort: its failure leaves the block null rather
    # than sinking the card (the quote is the only primary read).
    card = GetTickerCard(
        _FakeQuotes(),
        _FakePerformance(error=error),
    ).run("MU", include=["dividend", "performance"])

    assert card.performance is None  # swallowed, not raised
    assert card.quote.symbol == "MU"  # the card still serves


# ──────────────────────── the exchange lazy fill + anchor name ────────────────────────


def test_stored_facts_are_served_without_vendor_calls():
    stocks = _FakeStocks()
    repo = _FakeRepo(name="Micron Technology", exchange="NASDAQ")

    card = GetTickerCard(
        _FakeQuotes(), stocks=stocks, repository=repo
    ).run("MU")

    assert card.name == "Micron Technology"  # served off the anchor
    assert card.exchange == "NASDAQ"
    assert stocks.calls == []  # stored -> the full snapshot is never fetched
    assert repo.exchange_saves == []


def test_exchange_miss_fetches_once_and_stores():
    # The exchange is the one fact the card still lazily fills: a first view pays one
    # full-snapshot call to learn it, then it lives on the row. The name is anchor-only
    # now (no profile cold-miss fill), so an empty row serves a null name.
    stocks = _FakeStocks(exchange="NASDAQ")
    repo = _FakeRepo()

    card = GetTickerCard(
        _FakeQuotes(), stocks=stocks, repository=repo
    ).run("MU")

    assert card.name is None  # no stored name, and no profile fallback anymore
    assert card.exchange == "NASDAQ"
    assert stocks.calls == ["MU"]  # one full-snapshot call to learn the exchange
    assert repo.name_saves == []  # the card never fills the name now
    assert repo.exchange_saves == [("MU", "NASDAQ")]  # ...then it lives on the row


def test_exchange_fetch_failure_never_sinks_the_card():
    stocks = _FakeStocks(error=StockDataUnavailable("MU", "alpaca down"))
    repo = _FakeRepo()

    card = GetTickerCard(
        _FakeQuotes(), stocks=stocks, repository=repo
    ).run("MU")

    assert card.name is None  # no stored name
    assert card.exchange is None  # swallowed, not raised
    assert repo.exchange_saves == []


def test_facts_unknown_at_the_vendors_are_not_stored():
    stocks = _FakeStocks(exchange=None)
    repo = _FakeRepo()

    card = GetTickerCard(
        _FakeQuotes(), stocks=stocks, repository=repo
    ).run("MU")

    assert card.name is None  # no stored name on the row
    assert card.exchange is None  # the feed didn't know it either
    assert repo.name_saves == []  # nothing learned -> nothing written
    assert repo.exchange_saves == []


def test_facts_absent_without_wiring():
    # No repository (or vendors): the card still serves, the facts simply null — the
    # name too, now that it's anchor-only (no profile fallback).
    card = GetTickerCard(_FakeQuotes()).run("MU")
    assert card.name is None
    assert card.exchange is None


def test_quote_failure_propagates():
    # The quote is primary — the endpoint maps this to HTTP, nothing is swallowed.
    quotes = _FakeQuotes(error=StockDataUnavailable("MU", "alpaca down"))
    with pytest.raises(StockDataUnavailable):
        GetTickerCard(quotes).run("MU")


# ──────────────────────── the trailing P/E (consensus TTM) ────────────────────────


def test_metrics_carries_the_trailing_pe_off_the_quarterly_ttm():
    # Four reported quarters at 1.5 + 2.0 + 2.5 + 3.0 = a 9.0 TTM against a
    # 100.0 quote: the card's trailing multiple, on the consensus basis.
    earnings = _FakeEarnings(_four_quarters(1.5, 2.0, 2.5, 3.0))

    card = GetTickerCard(
        _FakeQuotes(price=100.0), earnings=earnings
    ).run("MU", include=["metrics"])

    assert earnings.calls == ["MU"]
    assert card.valuation.ttm_eps == pytest.approx(9.0)
    assert card.valuation.trailing_pe == pytest.approx(11.11)


def test_unrequested_metrics_cost_no_earnings_call():
    earnings = _FakeEarnings(_four_quarters(1.5, 2.0, 2.5, 3.0))

    card = GetTickerCard(
        _FakeQuotes(), earnings=earnings
    ).run("MU")

    assert earnings.calls == []  # pay-per-use, like the fundamentals read
    assert card.valuation is None


def test_too_few_cached_quarters_yield_a_null_trailing_pe():
    # An uncovered (or partially covered) symbol is a valid read — the multiple
    # is simply absent until the quarterly slice holds a full trailing year.
    earnings = _FakeEarnings(_four_quarters(2.5, 3.0))

    card = GetTickerCard(
        _FakeQuotes(), earnings=earnings
    ).run("MU", include=["metrics"])

    assert card.valuation.ttm_eps is None
    assert card.valuation.trailing_pe is None


@pytest.mark.parametrize(
    "error",
    [StockNotFound("MU"), StockDataUnavailable("MU", "yahoo blocked")],
)
def test_earnings_failure_never_sinks_the_card(error):
    # Best-effort even when requested: a cold cache miss goes live to Yahoo,
    # and a blocked fetch must degrade to a null multiple, not a failed card.
    earnings = _FakeEarnings(error=error)

    card = GetTickerCard(
        _FakeQuotes(), earnings=earnings
    ).run("MU", include=["metrics"])

    assert card.valuation.trailing_pe is None
    assert card.quote.symbol == "MU"  # the card still serves


def test_unwired_earnings_provider_leaves_the_trailing_pe_none():
    card = GetTickerCard(_FakeQuotes()).run(
        "MU", include=["metrics"]
    )
    assert card.valuation.ttm_eps is None
    assert card.valuation.trailing_pe is None


def test_metrics_carries_the_fcf_multiples_off_the_anchor():
    # FCF/OCF per share come from the stored anchor (the annual slice's write), not the
    # fundamentals call, and are priced at the live quote: $4 FCF/share and $6 OCF/share
    # against $100 is a 25x P/FCF, a 4% FCF yield and a 6% OCF yield.
    repo = _FakeRepo(fcf_per_share=4.0, ocf_per_share=6.0)

    card = GetTickerCard(
        _FakeQuotes(price=100.0), repository=repo
    ).run("MU", include=["metrics"])

    assert card.valuation.fcf_per_share == pytest.approx(4.0)
    assert card.valuation.ocf_per_share == pytest.approx(6.0)
    assert card.valuation.price_to_fcf == pytest.approx(25.0)
    assert card.valuation.fcf_yield == pytest.approx(4.0)
    assert card.valuation.ocf_yield == pytest.approx(6.0)


def test_metrics_carries_enterprise_value_and_ev_ebitda_off_the_anchor():
    # The EV inputs (shares/debt/cash/EBITDA) come from the stored anchor (the fundamentals
    # slice's write), priced at the live quote: $100 x 1B shares + $20B debt - $5B cash =
    # $115B EV, over $10B EBITDA -> 11.5x. Rides the same anchor read as the FCF/margins legs.
    repo = _FakeRepo(
        shares_outstanding=1_000_000_000.0,
        total_debt=20_000_000_000.0,
        cash_and_equivalents=5_000_000_000.0,
        ebitda=10_000_000_000.0,
    )

    card = GetTickerCard(
        _FakeQuotes(price=100.0), repository=repo
    ).run("MU", include=["metrics"])

    assert card.valuation.enterprise_value == pytest.approx(115_000_000_000.0)
    assert card.valuation.ev_to_ebitda == pytest.approx(11.5)


def test_fcf_multiples_and_trailing_pe_ride_the_anchor_and_earnings():
    # The FCF leg is read off the anchor (the same read that serves the growth pair), and
    # the trailing P/E off the separate earnings read — neither depends on a live
    # fundamentals vendor, so both serve together straight off the DB.
    repo = _FakeRepo(fcf_per_share=4.0, ocf_per_share=6.0)
    earnings = _FakeEarnings(_four_quarters(1.5, 2.0, 2.5, 3.0))

    card = GetTickerCard(
        _FakeQuotes(price=100.0),
        repository=repo,
        earnings=earnings,
    ).run("MU", include=["metrics"])

    assert card.valuation.fcf_yield == pytest.approx(4.0)
    assert card.valuation.ocf_yield == pytest.approx(6.0)
    assert card.valuation.trailing_pe == pytest.approx(11.11)


def test_fcf_multiples_are_none_when_the_anchor_lacks_cash_figures():
    # Best-effort: an uncovered symbol (the annual slice hasn't reached it, so the anchor
    # carries no per-share cash) yields null FCF/OCF reads without sinking the card or the
    # trailing P/E (which rides the separate earnings read).
    earnings = _FakeEarnings(_four_quarters(1.5, 2.0, 2.5, 3.0))

    card = GetTickerCard(
        _FakeQuotes(price=100.0), earnings=earnings
    ).run("MU", include=["metrics"])

    assert card.valuation.fcf_per_share is None
    assert card.valuation.ocf_per_share is None
    assert card.valuation.price_to_fcf is None
    assert card.valuation.fcf_yield is None
    assert card.valuation.ocf_yield is None
    assert card.valuation.trailing_pe == pytest.approx(11.11)  # the card still serves


def _card_with_options(options: _FakeOptions, include=("options_metrics",)):
    return GetTickerCard(
        _FakeQuotes(price=100.0),
        options=options,
        today=lambda: _TODAY,
    ).run("MU", include=list(include))


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
    card = GetTickerCard(_FakeQuotes()).run(
        "MU", include=["options_metrics"]
    )
    assert card.options_metrics is None
