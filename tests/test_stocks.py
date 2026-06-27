"""Tests for the stocks vertical slice: entity rules, use case, and the API.

Everything here runs offline. The use case depends on the StockDataProvider
port, so we inject a hand-written FakeProvider instead of mocking Alpaca or
the network — that's the payoff of the clean-architecture layering.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.stocks.chart_window import ChartRange, resolve_window
from app.stocks.entities import (
    Candle,
    CandleSeries,
    CompanyProfile,
    Constituent,
    EarningsHistory,
    EarningsMetrics,
    EarningsSurprise,
    KeyMetrics,
    Logo,
    Quote,
    SectorPerformance,
    Stock,
    StockFundamentals,
    StockIndex,
    StockPerformance,
    Timeframe,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.indicators import RsiSignal
from app.stocks.ports import (
    CandleProvider,
    CompanyProfileProvider,
    ConstituentRepository,
    EarningsHistoryProvider,
    LogoProvider,
    QuoteBatchProvider,
    SectorPerformanceProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.router import (
    get_screener,
    get_sector_performance,
    get_stock_candles,
    get_stock_earnings,
    get_stock_info,
    get_stock_logo,
    get_stock_quote,
    get_stock_rsi,
)
from app.stocks.use_cases import (
    GetSectorPerformance,
    GetStockCandles,
    GetStockEarnings,
    GetStockInfo,
    GetStockLogo,
    GetStockQuote,
    GetStockRsi,
    ScreenStocks,
)


class FakeProvider(StockDataProvider):
    """Returns/raises whatever the test configured; records calls."""

    def __init__(self, stock: Stock | None = None, raises: Exception | None = None):
        self._stock = stock
        self._raises = raises
        self.received: list[str] = []

    def get_stock(self, symbol: str) -> Stock:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._stock is not None
        return self._stock


class FakeQuoteProvider(StockQuoteProvider):
    """Returns/raises whatever the test configured; records calls."""

    def __init__(self, quote: Quote | None = None, raises: Exception | None = None):
        self._quote = quote
        self._raises = raises
        self.received: list[str] = []

    def get_quote(self, symbol: str) -> Quote:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._quote is not None
        return self._quote


class FakeLogoProvider(LogoProvider):
    """Returns/raises whatever the test configured; records calls."""

    def __init__(self, logo: Logo | None = None, raises: Exception | None = None):
        self._logo = logo
        self._raises = raises
        self.received: list[str] = []

    def get_logo(self, symbol: str) -> Logo:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._logo is not None
        return self._logo


class FakePerformanceProvider(StockPerformanceProvider):
    """Returns/raises whatever the test configured; records calls."""

    def __init__(self, performance=None, raises=None):
        self._performance = performance
        self._raises = raises
        self.received: list[str] = []

    def get_performance(self, symbol: str) -> StockPerformance:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._performance is not None
        return self._performance


class FakeFundamentalsProvider(StockFundamentalsProvider):
    """Returns/raises whatever the test configured; records calls."""

    def __init__(self, fundamentals=None, raises=None):
        self._fundamentals = fundamentals
        self._raises = raises
        self.received: list[str] = []

    def get_fundamentals(self, symbol: str) -> StockFundamentals:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._fundamentals is not None
        return self._fundamentals


class FakeProfileProvider(CompanyProfileProvider):
    """Returns/raises whatever the test configured; records calls."""

    def __init__(self, profile=None, raises=None):
        self._profile = profile
        self._raises = raises
        self.received: list[str] = []

    def get_profile(self, symbol: str) -> CompanyProfile:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._profile is not None
        return self._profile


class FakeCandleProvider(CandleProvider):
    """Returns/raises whatever the test configured; records the call args."""

    def __init__(
        self, series: CandleSeries | None = None, raises: Exception | None = None
    ):
        self._series = series
        self._raises = raises
        self.received: list[tuple] = []

    def get_candles(self, symbol, timeframe, *, start=None, end=None) -> CandleSeries:
        self.received.append((symbol, timeframe, start, end))
        if self._raises is not None:
            raise self._raises
        assert self._series is not None
        return self._series


class FakeSectorProvider(SectorPerformanceProvider):
    """Returns/raises whatever the test configured; counts calls."""

    def __init__(self, sectors=None, raises: Exception | None = None):
        self._sectors = sectors
        self._raises = raises
        self.calls = 0

    def get_sector_performance(self) -> list[SectorPerformance]:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._sectors is not None
        return self._sectors


class FakeEarningsProvider(EarningsHistoryProvider):
    """Returns/raises whatever the test configured; records the call args."""

    def __init__(self, history=None, raises: Exception | None = None):
        self._history = history
        self._raises = raises
        self.received: list[tuple] = []

    def get_earnings_history(self, symbol: str, *, limit: int) -> EarningsHistory:
        self.received.append((symbol, limit))
        if self._raises is not None:
            raise self._raises
        assert self._history is not None
        return self._history


def a_logo(content: bytes = b"\x89PNG\r\n", media_type: str = "image/png") -> Logo:
    return Logo(content=content, media_type=media_type)


def a_candle(**overrides) -> Candle:
    base = dict(
        timestamp=datetime(2026, 6, 18, tzinfo=timezone.utc),
        open=298.44, high=300.56, low=295.63, close=299.50, volume=1278873,
    )
    base.update(overrides)
    return Candle(**base)


def a_series(candles=None, timeframe: Timeframe = Timeframe.DAY_1) -> CandleSeries:
    if candles is None:
        candles = (a_candle(),)
    return CandleSeries(symbol="AAPL", timeframe=timeframe, candles=tuple(candles))


def a_rising_series(
    n: int = 4, start_close: float = 100.0, timeframe: Timeframe = Timeframe.DAY_1
) -> CandleSeries:
    """A series of strictly rising closes — all gains, so RSI pins to 100."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(
        a_candle(close=start_close + i, timestamp=base + timedelta(days=i))
        for i in range(n)
    )
    return a_series(candles, timeframe=timeframe)


def a_stock(**overrides) -> Stock:
    base = dict(
        symbol="AAPL", name="Apple Inc.", exchange="NASDAQ", price=297.86,
        open=298.44, high=300.56, low=295.635, previous_close=296.07,
        volume=1278873, bid=283.52, ask=313.43,
        as_of=datetime(2026, 6, 18, 19, 59, 59, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Stock(**base)


def a_quote(**overrides) -> Quote:
    base = dict(
        symbol="AAPL", price=297.86, previous_close=296.07,
        bid=283.52, ask=313.43,
        as_of=datetime(2026, 6, 18, 19, 59, 59, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Quote(**base)


def a_performance(**overrides) -> StockPerformance:
    base = dict(
        one_week=1.2, one_month=-0.4, three_month=5.1,
        six_month=8.7, ytd=12.3, one_year=21.0,
    )
    base.update(overrides)
    return StockPerformance(**base)


def a_key_metrics(**overrides) -> KeyMetrics:
    base = dict(
        # Valuation / health / market (stay on the stock snapshot)
        pe=28.5, pb=45.2, ps=7.1, beta=1.2,
        current_ratio=0.9, debt_to_equity=1.5,
        week_52_high=320.0, week_52_low=210.0,
        # Earnings-flavored (relocated to the earnings endpoint)
        eps=6.1, eps_growth_yoy=12.0, revenue_growth_yoy=8.0,
        gross_margin=44.0, operating_margin=30.0, net_margin=25.0,
        roe=150.0, roic=40.0, payout_ratio=15.0,
    )
    base.update(overrides)
    return KeyMetrics(**base)


def a_fundamentals(**overrides) -> StockFundamentals:
    base = dict(
        market_cap=3_120_000_000_000.0, dividend_per_share=1.0, dividend_yield=0.42,
        metrics=a_key_metrics(),
    )
    base.update(overrides)
    return StockFundamentals(**base)


def a_profile(
    description: str = "Apple Inc. designs and sells consumer electronics.",
    name: str | None = None,
) -> CompanyProfile:
    return CompanyProfile(description=description, name=name)


def a_sector(**overrides) -> SectorPerformance:
    base = dict(
        sector="Technology", symbol="XLK", price=255.0, previous_close=250.0,
        as_of=datetime(2026, 6, 18, 19, 59, 59, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return SectorPerformance(**base)


def a_surprise(**overrides) -> EarningsSurprise:
    base = dict(
        period=date(2026, 3, 31), fiscal_year=2026, fiscal_quarter=1,
        actual=2.18, estimate=2.10, surprise=0.08, surprise_percent=3.81,
    )
    base.update(overrides)
    return EarningsSurprise(**base)


def a_history(quarters=None, symbol: str = "AAPL") -> EarningsHistory:
    if quarters is None:
        quarters = (a_surprise(),)
    return EarningsHistory(symbol=symbol, quarters=tuple(quarters))


# --------------------------- entity rules (pure) ---------------------------

def test_entity_change_and_percent():
    s = a_stock(price=110.0, previous_close=100.0)
    assert s.change == 10.0
    assert s.change_percent == 10.0


def test_entity_change_none_without_previous_close():
    s = a_stock(previous_close=None)
    assert s.change is None
    assert s.change_percent is None


def test_entity_change_percent_guards_zero_division():
    assert a_stock(previous_close=0).change_percent is None


def test_entity_spread():
    assert a_stock(bid=283.52, ask=313.43).spread == 29.91
    assert a_stock(bid=None).spread is None


def test_quote_entity_mirrors_stock_change_rules():
    # Quote duplicates Stock's change/spread rules on purpose — they must agree.
    q = a_quote(price=110.0, previous_close=100.0)
    assert q.change == 10.0
    assert q.change_percent == 10.0
    assert q.spread == 29.91
    assert a_quote(previous_close=None).change is None
    assert a_quote(previous_close=0).change_percent is None
    assert a_quote(bid=None).spread is None


def test_key_metrics_peg_divides_pe_by_growth():
    assert KeyMetrics(pe=30.0, eps_growth_yoy=15.0).peg == 2.0
    assert KeyMetrics(pe=28.5, eps_growth_yoy=10.0).peg == 2.85  # rounded to 2dp


@pytest.mark.parametrize(
    "pe, growth",
    [
        (30.0, None),   # growth unknown
        (None, 15.0),   # P/E unknown
        (30.0, 0.0),    # zero growth -> undefined
        (30.0, -5.0),   # shrinking earnings -> meaningless
        (-12.0, 15.0),  # negative P/E (losses) -> meaningless
    ],
)
def test_key_metrics_peg_none_when_inputs_missing_or_nonpositive(pe, growth):
    assert KeyMetrics(pe=pe, eps_growth_yoy=growth).peg is None


def test_earnings_surprise_beat_flag():
    assert a_surprise(actual=2.0, estimate=1.5).beat is True   # beat
    assert a_surprise(actual=1.5, estimate=1.5).beat is True   # met counts as beat
    assert a_surprise(actual=1.0, estimate=1.5).beat is False  # miss
    assert a_surprise(actual=None, estimate=1.5).beat is None  # unknowable
    assert a_surprise(actual=2.0, estimate=None).beat is None


def test_earnings_history_beat_rate_ignores_unscored_quarters():
    history = a_history((
        a_surprise(actual=2.0, estimate=1.5),   # beat
        a_surprise(actual=1.0, estimate=1.5),   # miss
        a_surprise(actual=2.0, estimate=1.5),   # beat
        a_surprise(actual=None, estimate=None), # unscored -> excluded
    ))
    assert history.scored == 3
    assert history.beats == 2
    assert history.beat_rate == 66.7  # 2/3, one decimal


def test_earnings_history_beat_rate_none_when_nothing_scoreable():
    history = a_history((a_surprise(actual=None, estimate=None),))
    assert history.scored == 0
    assert history.beats == 0
    assert history.beat_rate is None


def test_earnings_metrics_projects_earnings_fields_from_key_metrics():
    em = EarningsMetrics.from_key_metrics(a_key_metrics())
    # carries the earnings-flavored slice...
    assert em.eps == 6.1
    assert em.net_margin == 25.0
    assert em.eps_growth_yoy == 12.0
    assert em.roic == 40.0
    assert em.payout_ratio == 15.0
    # ...and nothing valuation-flavored leaks across (it has no such fields)
    assert not hasattr(em, "pe")
    assert not hasattr(em, "beta")


def test_earnings_metrics_none_without_earnings_fields():
    # KeyMetrics present but only valuation fields set -> nothing to carry
    assert EarningsMetrics.from_key_metrics(KeyMetrics(pe=20.0, beta=1.1)) is None
    assert EarningsMetrics.from_key_metrics(None) is None


def test_candle_is_bullish():
    assert a_candle(open=100.0, close=110.0).is_bullish is True   # up -> green
    assert a_candle(open=110.0, close=100.0).is_bullish is False  # down -> red
    assert a_candle(open=100.0, close=100.0).is_bullish is True   # doji -> green


# --------------------------- chart window (range -> start/end) ---------------------------

def test_resolve_window_lookback():
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    start, end = resolve_window(ChartRange.MONTH_1, now=now)
    assert end == now
    assert start == now - (now - start)  # sanity
    assert (now - start).days == 31


def test_resolve_window_max_has_no_start():
    now = datetime(2026, 6, 21, tzinfo=timezone.utc)
    start, end = resolve_window(ChartRange.MAX, now=now)
    assert start is None
    assert end == now


def test_resolve_window_ytd_starts_at_jan_1():
    now = datetime(2026, 6, 21, 12, 30, tzinfo=timezone.utc)
    start, end = resolve_window(ChartRange.YTD, now=now)
    assert start == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert end == now


# --------------------------- use case ---------------------------

def test_use_case_normalizes_symbol():
    fake = FakeProvider(stock=a_stock())
    GetStockInfo(fake).execute("  aapl ")
    assert fake.received == ["AAPL"]


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_use_case_rejects_invalid_symbols(bad):
    fake = FakeProvider(stock=a_stock())
    with pytest.raises(ValueError):
        GetStockInfo(fake).execute(bad)
    assert fake.received == []  # provider untouched on invalid input


def test_use_case_propagates_not_found():
    fake = FakeProvider(raises=StockNotFound("ZZZZ"))
    with pytest.raises(StockNotFound):
        GetStockInfo(fake).execute("ZZZZ")


def test_use_case_merges_enrichment():
    info = GetStockInfo(
        FakeProvider(stock=a_stock()),
        FakePerformanceProvider(a_performance()),
        FakeFundamentalsProvider(a_fundamentals()),
        FakeProfileProvider(a_profile()),
    )
    stock = info.execute("AAPL")
    assert stock.market_cap == 3_120_000_000_000.0
    assert stock.dividend_per_share == 1.0
    assert stock.dividend_yield == 0.42
    assert stock.performance.one_year == 21.0
    assert stock.metrics.pe == 28.5
    assert stock.metrics.beta == 1.2
    assert stock.description == "Apple Inc. designs and sells consumer electronics."


def test_use_case_prefers_profile_name_over_feed_name():
    # The price feed gives the full legal title; the profile vendor's clean name
    # wins when present.
    info = GetStockInfo(
        FakeProvider(stock=a_stock(name="Apple Inc. Common Stock")),
        profile_provider=FakeProfileProvider(a_profile(name="Apple Inc.")),
    )
    assert info.execute("AAPL").name == "Apple Inc."


def test_use_case_keeps_feed_name_when_profile_name_absent():
    # No clean name from the vendor -> fall back to the feed's name, unchanged.
    info = GetStockInfo(
        FakeProvider(stock=a_stock(name="Apple Inc. Common Stock")),
        profile_provider=FakeProfileProvider(a_profile(name=None)),
    )
    assert info.execute("AAPL").name == "Apple Inc. Common Stock"


def test_use_case_keeps_feed_name_when_profile_unconfigured():
    # No profile provider at all -> the feed's name stands.
    stock = GetStockInfo(
        FakeProvider(stock=a_stock(name="Apple Inc. Common Stock"))
    ).execute("AAPL")
    assert stock.name == "Apple Inc. Common Stock"


def test_use_case_without_enrichment_leaves_fields_none():
    stock = GetStockInfo(FakeProvider(stock=a_stock())).execute("AAPL")
    assert stock.market_cap is None
    assert stock.dividend_yield is None
    assert stock.performance is None
    assert stock.metrics is None
    assert stock.description is None


def test_use_case_enrichment_is_best_effort():
    info = GetStockInfo(
        FakeProvider(stock=a_stock()),
        FakePerformanceProvider(raises=StockDataUnavailable("AAPL", "boom")),
        FakeFundamentalsProvider(raises=StockNotFound("AAPL")),
        FakeProfileProvider(raises=StockDataUnavailable("AAPL", "boom")),
    )
    stock = info.execute("AAPL")  # enrichment failures must not raise
    assert stock.price == 297.86
    assert stock.performance is None
    assert stock.market_cap is None
    assert stock.description is None


def test_quote_use_case_normalizes_symbol():
    fake = FakeQuoteProvider(quote=a_quote())
    GetStockQuote(fake).execute("  aapl ")
    assert fake.received == ["AAPL"]


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_quote_use_case_rejects_invalid_symbols(bad):
    fake = FakeQuoteProvider(quote=a_quote())
    with pytest.raises(ValueError):
        GetStockQuote(fake).execute(bad)
    assert fake.received == []  # provider untouched on invalid input


def test_quote_use_case_propagates_not_found():
    fake = FakeQuoteProvider(raises=StockNotFound("ZZZZ"))
    with pytest.raises(StockNotFound):
        GetStockQuote(fake).execute("ZZZZ")


def test_logo_use_case_normalizes_symbol():
    fake = FakeLogoProvider(logo=a_logo(content=b"PNG"))
    assert GetStockLogo(fake).execute("  aapl ").content == b"PNG"
    assert fake.received == ["AAPL"]


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_logo_use_case_rejects_invalid_symbols(bad):
    fake = FakeLogoProvider(logo=a_logo())
    with pytest.raises(ValueError):
        GetStockLogo(fake).execute(bad)
    assert fake.received == []  # provider untouched on invalid input


def test_candles_use_case_normalizes_symbol_and_forwards_window():
    fake = FakeCandleProvider(series=a_series())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    GetStockCandles(fake).execute("  aapl ", Timeframe.HOUR_1, start=start, end=end)
    assert fake.received == [("AAPL", Timeframe.HOUR_1, start, end)]


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_candles_use_case_rejects_invalid_symbols(bad):
    fake = FakeCandleProvider(series=a_series())
    with pytest.raises(ValueError):
        GetStockCandles(fake).execute(bad, Timeframe.DAY_1)
    assert fake.received == []  # provider untouched on invalid input


def test_candles_use_case_rejects_inverted_window():
    fake = FakeCandleProvider(series=a_series())
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        GetStockCandles(fake).execute("AAPL", Timeframe.DAY_1, start=start, end=end)
    assert fake.received == []


def test_candles_use_case_propagates_not_found():
    fake = FakeCandleProvider(raises=StockNotFound("ZZZZ"))
    with pytest.raises(StockNotFound):
        GetStockCandles(fake).execute("ZZZZ", Timeframe.DAY_1)


# --------------------------- RSI use case ---------------------------

def test_rsi_use_case_normalizes_symbol_and_forwards_window():
    fake = FakeCandleProvider(series=a_rising_series())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    GetStockRsi(fake).execute(
        "  aapl ", Timeframe.HOUR_1, period=2, start=start, end=end
    )
    assert fake.received == [("AAPL", Timeframe.HOUR_1, start, end)]


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_rsi_use_case_rejects_invalid_symbols(bad):
    fake = FakeCandleProvider(series=a_rising_series())
    with pytest.raises(ValueError):
        GetStockRsi(fake).execute(bad, Timeframe.DAY_1)
    assert fake.received == []  # provider untouched on invalid input


def test_rsi_use_case_rejects_inverted_window():
    fake = FakeCandleProvider(series=a_rising_series())
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        GetStockRsi(fake).execute("AAPL", Timeframe.DAY_1, start=start, end=end)
    assert fake.received == []


def test_rsi_use_case_propagates_not_found():
    fake = FakeCandleProvider(raises=StockNotFound("ZZZZ"))
    with pytest.raises(StockNotFound):
        GetStockRsi(fake).execute("ZZZZ", Timeframe.DAY_1)


def test_rsi_use_case_computes_from_fetched_candles():
    # Strictly rising closes -> all gains -> RSI pinned at 100 -> overbought.
    result = GetStockRsi(FakeCandleProvider(series=a_rising_series())).execute(
        "AAPL", Timeframe.DAY_1, period=2
    )
    assert result.latest.value == 100.0
    assert result.signal is RsiSignal.OVERBOUGHT


# --------------------------- sector use case ---------------------------

def test_sector_entity_change_and_percent():
    s = a_sector(price=110.0, previous_close=100.0)
    assert s.change == 10.0
    assert s.change_percent == 10.0
    assert a_sector(previous_close=None).change_percent is None


def test_sector_use_case_ranks_best_performer_first():
    tech = a_sector(sector="Technology", price=110.0, previous_close=100.0)    # +10%
    energy = a_sector(sector="Energy", price=95.0, previous_close=100.0)       # -5%
    health = a_sector(sector="Health Care", price=102.0, previous_close=100.0) # +2%
    ranked = GetSectorPerformance(FakeSectorProvider([energy, health, tech])).execute()
    assert [s.sector for s in ranked] == ["Technology", "Health Care", "Energy"]


def test_sector_use_case_sorts_missing_percent_last():
    up = a_sector(sector="Technology", price=110.0, previous_close=100.0)  # +10%
    unknown = a_sector(sector="Energy", previous_close=None)              # no percent
    ranked = GetSectorPerformance(FakeSectorProvider([unknown, up])).execute()
    assert [s.sector for s in ranked] == ["Technology", "Energy"]


def test_sector_use_case_propagates_not_found():
    fake = FakeSectorProvider(raises=StockNotFound("sectors"))
    with pytest.raises(StockNotFound):
        GetSectorPerformance(fake).execute()


def test_earnings_use_case_normalizes_symbol_and_forwards_limit():
    fake = FakeEarningsProvider(a_history())
    GetStockEarnings(fake).execute("  aapl ", limit=8)
    assert fake.received == [("AAPL", 8)]


def test_earnings_use_case_defaults_to_four_quarters():
    fake = FakeEarningsProvider(a_history())
    GetStockEarnings(fake).execute("AAPL")
    assert fake.received == [("AAPL", 4)]


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "TOOLONG"])
def test_earnings_use_case_rejects_invalid_symbols(bad):
    fake = FakeEarningsProvider(a_history())
    with pytest.raises(ValueError):
        GetStockEarnings(fake).execute(bad)
    assert fake.received == []  # provider untouched on invalid input


def test_earnings_use_case_rejects_non_positive_limit():
    fake = FakeEarningsProvider(a_history())
    with pytest.raises(ValueError):
        GetStockEarnings(fake).execute("AAPL", limit=0)
    assert fake.received == []


def test_earnings_use_case_propagates_not_found():
    fake = FakeEarningsProvider(raises=StockNotFound("ZZZZ"))
    with pytest.raises(StockNotFound):
        GetStockEarnings(fake).execute("ZZZZ")


def test_earnings_use_case_attaches_metrics_from_fundamentals():
    history = GetStockEarnings(
        FakeEarningsProvider(a_history()),
        FakeFundamentalsProvider(a_fundamentals()),
    ).execute("AAPL")
    assert history.metrics is not None
    assert history.metrics.eps == 6.1
    assert history.metrics.net_margin == 25.0


def test_earnings_use_case_metrics_none_without_fundamentals_provider():
    history = GetStockEarnings(FakeEarningsProvider(a_history())).execute("AAPL")
    assert history.metrics is None


def test_earnings_use_case_metrics_best_effort_when_fundamentals_fail():
    # Fundamentals are enrichment: a failure leaves the beat history intact.
    fake = FakeEarningsProvider(a_history())
    history = GetStockEarnings(
        fake, FakeFundamentalsProvider(raises=StockDataUnavailable("AAPL", "boom"))
    ).execute("AAPL")
    assert history.metrics is None
    assert history.quarters  # primary data survived


# --------------------------- API ---------------------------

@pytest.fixture
def make_client():
    def _make(
        provider: StockDataProvider | None = None,
        logo_provider: LogoProvider | None = None,
        performance_provider: StockPerformanceProvider | None = None,
        fundamentals_provider: StockFundamentalsProvider | None = None,
        profile_provider: CompanyProfileProvider | None = None,
        candle_provider: CandleProvider | None = None,
        rsi_provider: CandleProvider | None = None,
        sector_provider: SectorPerformanceProvider | None = None,
        earnings_provider: EarningsHistoryProvider | None = None,
        quote_provider: StockQuoteProvider | None = None,
    ) -> TestClient:
        if provider is not None:
            app.dependency_overrides[get_stock_info] = lambda: GetStockInfo(
                provider, performance_provider, fundamentals_provider, profile_provider
            )
        if quote_provider is not None:
            app.dependency_overrides[get_stock_quote] = (
                lambda: GetStockQuote(quote_provider)
            )
        if logo_provider is not None:
            app.dependency_overrides[get_stock_logo] = lambda: GetStockLogo(logo_provider)
        if candle_provider is not None:
            app.dependency_overrides[get_stock_candles] = (
                lambda: GetStockCandles(candle_provider)
            )
        if rsi_provider is not None:
            app.dependency_overrides[get_stock_rsi] = lambda: GetStockRsi(rsi_provider)
        if sector_provider is not None:
            app.dependency_overrides[get_sector_performance] = (
                lambda: GetSectorPerformance(sector_provider)
            )
        if earnings_provider is not None:
            app.dependency_overrides[get_stock_earnings] = (
                lambda: GetStockEarnings(earnings_provider, fundamentals_provider)
            )
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def test_get_stock_returns_200_with_computed_fields(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    r = client.get("/stocks/AAPL")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["name"] == "Apple Inc."
    assert body["price"] == 297.86
    assert body["change"] == 1.79          # entity rule, surfaced by the presenter
    assert body["change_percent"] == 0.6
    assert body["spread"] == 29.91


def test_get_stock_normalizes_lowercase(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    assert client.get("/stocks/aapl").json()["symbol"] == "AAPL"


def test_get_stock_invalid_symbol_400(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    assert client.get("/stocks/123").status_code == 400


def test_get_stock_unknown_symbol_404(make_client):
    client = make_client(FakeProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ZZZZ").status_code == 404


def test_get_stock_upstream_failure_502(make_client):
    client = make_client(FakeProvider(raises=StockDataUnavailable("AAPL", "boom")))
    assert client.get("/stocks/AAPL").status_code == 502


def test_get_stock_includes_enrichment_with_alias_keys(make_client):
    client = make_client(
        FakeProvider(stock=a_stock()),
        performance_provider=FakePerformanceProvider(a_performance()),
        fundamentals_provider=FakeFundamentalsProvider(a_fundamentals()),
        profile_provider=FakeProfileProvider(a_profile()),
    )
    body = client.get("/stocks/AAPL").json()
    assert body["market_cap"] == 3_120_000_000_000.0
    assert body["dividend_per_share"] == 1.0
    assert body["dividend_yield"] == 0.42
    assert body["description"] == "Apple Inc. designs and sells consumer electronics."
    # nested performance is serialized with finance-style JSON keys
    assert body["performance"] == {
        "1w": 1.2, "1m": -0.4, "3m": 5.1, "6m": 8.7, "ytd": 12.3, "1y": 21.0,
    }
    # nested key metrics ride along on the same fundamentals payload — only the
    # valuation/health/market slice; earnings-flavored metrics moved to /earnings
    assert body["metrics"]["pe"] == 28.5
    assert body["metrics"]["beta"] == 1.2
    assert body["metrics"]["week_52_high"] == 320.0
    assert body["metrics"]["ps"] == 7.1
    assert body["metrics"]["debt_to_equity"] == 1.5
    for moved in ("eps", "roe", "roic", "net_margin", "eps_growth_yoy", "payout_ratio"):
        assert moved not in body["metrics"], moved


def test_get_stock_enrichment_best_effort_returns_200(make_client):
    client = make_client(
        FakeProvider(stock=a_stock()),
        performance_provider=FakePerformanceProvider(
            raises=StockDataUnavailable("AAPL", "boom")
        ),
        fundamentals_provider=FakeFundamentalsProvider(raises=StockNotFound("AAPL")),
        profile_provider=FakeProfileProvider(raises=StockDataUnavailable("AAPL", "x")),
    )
    r = client.get("/stocks/AAPL")
    assert r.status_code == 200, r.text  # price survives enrichment failures
    body = r.json()
    assert body["price"] == 297.86
    assert body["market_cap"] is None
    assert body["performance"] is None
    assert body["description"] is None


def test_get_stock_without_enrichment_providers_nulls_fields(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    body = client.get("/stocks/AAPL").json()
    assert body["market_cap"] is None
    assert body["dividend_per_share"] is None
    assert body["performance"] is None
    assert body["metrics"] is None
    assert body["description"] is None


# --------------------------- quote endpoint ---------------------------

def test_get_quote_returns_200_with_computed_fields(make_client):
    client = make_client(quote_provider=FakeQuoteProvider(a_quote()))
    r = client.get("/stocks/AAPL/quote")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["price"] == 297.86
    assert body["change"] == 1.79              # entity rule via presenter
    assert body["change_percent"] == 0.6
    assert body["spread"] == 29.91
    # slim payload: none of the full stock's enrichment fields
    assert "market_cap" not in body
    assert "name" not in body


def test_get_quote_sets_short_cache_header(make_client):
    client = make_client(quote_provider=FakeQuoteProvider(a_quote()))
    r = client.get("/stocks/AAPL/quote")
    assert r.headers["cache-control"] == "public, max-age=2"


def test_get_quote_normalizes_lowercase(make_client):
    fake = FakeQuoteProvider(a_quote())
    client = make_client(quote_provider=fake)
    assert client.get("/stocks/aapl/quote").json()["symbol"] == "AAPL"
    assert fake.received == ["AAPL"]


def test_get_quote_invalid_symbol_400(make_client):
    client = make_client(quote_provider=FakeQuoteProvider(a_quote()))
    assert client.get("/stocks/123/quote").status_code == 400


def test_get_quote_unknown_symbol_404(make_client):
    client = make_client(quote_provider=FakeQuoteProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ZZZZ/quote").status_code == 404


def test_get_quote_upstream_failure_502(make_client):
    fake = FakeQuoteProvider(raises=StockDataUnavailable("AAPL", "boom"))
    client = make_client(quote_provider=fake)
    assert client.get("/stocks/AAPL/quote").status_code == 502


# --------------------------- logo endpoint ---------------------------

def test_get_logo_returns_png_bytes(make_client):
    client = make_client(logo_provider=FakeLogoProvider(a_logo(content=b"\x89PNG\r\n")))
    r = client.get("/stocks/AAPL/logo")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content == b"\x89PNG\r\n"


def test_get_logo_passes_through_media_type(make_client):
    svg = a_logo(content=b"<svg/>", media_type="image/svg+xml")
    client = make_client(logo_provider=FakeLogoProvider(svg))
    r = client.get("/stocks/AAPL/logo")
    assert r.headers["content-type"] == "image/svg+xml"


def test_get_logo_invalid_symbol_400(make_client):
    client = make_client(logo_provider=FakeLogoProvider(a_logo()))
    assert client.get("/stocks/123/logo").status_code == 400


def test_get_logo_missing_404(make_client):
    client = make_client(logo_provider=FakeLogoProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ZZZZ/logo").status_code == 404


def test_get_logo_upstream_failure_502(make_client):
    fake = FakeLogoProvider(raises=StockDataUnavailable("AAPL", "boom"))
    client = make_client(logo_provider=fake)
    assert client.get("/stocks/AAPL/logo").status_code == 502


# --------------------------- candles endpoint ---------------------------

def test_get_candles_returns_200_with_chart_shape(make_client):
    up = a_candle(open=100.0, close=110.0, timestamp=datetime(2026, 6, 18, tzinfo=timezone.utc))
    down = a_candle(open=110.0, close=105.0, timestamp=datetime(2026, 6, 19, tzinfo=timezone.utc))
    client = make_client(candle_provider=FakeCandleProvider(a_series((up, down))))
    r = client.get("/stocks/AAPL/candles")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["timeframe"] == "1Day"
    assert body["count"] == 2
    first = body["candles"][0]
    assert first["open"] == 100.0 and first["close"] == 110.0
    assert first["direction"] == "up"                     # green
    assert body["candles"][1]["direction"] == "down"      # red
    assert first["time"] == int(datetime(2026, 6, 18, tzinfo=timezone.utc).timestamp())


def test_get_candles_defaults_to_6m_daily(make_client):
    fake = FakeCandleProvider(a_series())
    client = make_client(candle_provider=fake)
    assert client.get("/stocks/AAPL/candles").status_code == 200
    symbol, timeframe, start, end = fake.received[0]
    assert symbol == "AAPL"
    assert timeframe is Timeframe.DAY_1                    # default timeframe
    assert start is not None and end is not None
    assert (end - start).days == 183                       # default range = 6M


def test_get_candles_honors_timeframe_and_range(make_client):
    fake = FakeCandleProvider(a_series(timeframe=Timeframe.HOUR_1))
    client = make_client(candle_provider=fake)
    r = client.get("/stocks/AAPL/candles", params={"timeframe": "1Hour", "range": "5D"})
    assert r.status_code == 200
    _, timeframe, start, end = fake.received[0]
    assert timeframe is Timeframe.HOUR_1
    assert (end - start).days == 5


def test_get_candles_explicit_window_overrides_range(make_client):
    fake = FakeCandleProvider(a_series())
    client = make_client(candle_provider=fake)
    r = client.get(
        "/stocks/AAPL/candles",
        params={"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
    )
    assert r.status_code == 200
    _, _, start, end = fake.received[0]
    assert start == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 2, 1, tzinfo=timezone.utc)


def test_get_candles_invalid_timeframe_422(make_client):
    client = make_client(candle_provider=FakeCandleProvider(a_series()))
    assert client.get("/stocks/AAPL/candles", params={"timeframe": "1Year"}).status_code == 422


def test_get_candles_invalid_symbol_400(make_client):
    client = make_client(candle_provider=FakeCandleProvider(a_series()))
    assert client.get("/stocks/123/candles").status_code == 400


def test_get_candles_inverted_window_400(make_client):
    client = make_client(candle_provider=FakeCandleProvider(a_series()))
    r = client.get(
        "/stocks/AAPL/candles",
        params={"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 400


def test_get_candles_unknown_symbol_404(make_client):
    client = make_client(candle_provider=FakeCandleProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ZZZZ/candles").status_code == 404


def test_get_candles_upstream_failure_502(make_client):
    fake = FakeCandleProvider(raises=StockDataUnavailable("AAPL", "boom"))
    client = make_client(candle_provider=fake)
    assert client.get("/stocks/AAPL/candles").status_code == 502


# --------------------------- RSI endpoint ---------------------------

def test_get_rsi_returns_200_with_signal(make_client):
    client = make_client(rsi_provider=FakeCandleProvider(a_rising_series()))
    r = client.get("/stocks/AAPL/rsi", params={"period": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["timeframe"] == "1Day"
    assert body["period"] == 2
    assert body["count"] == 2                  # 4 candles - period 2
    assert body["latest"] == 100.0             # all gains -> pinned high
    assert body["signal"] == "overbought"      # the take-profit band
    assert body["overbought"] == 70.0 and body["oversold"] == 30.0
    assert set(body["points"][0]) == {"time", "timestamp", "value"}


def test_get_rsi_defaults_to_6m_daily_period_14(make_client):
    fake = FakeCandleProvider(a_series())       # a single candle
    client = make_client(rsi_provider=fake)
    r = client.get("/stocks/AAPL/rsi")
    assert r.status_code == 200, r.text
    symbol, timeframe, start, end = fake.received[0]
    assert symbol == "AAPL"
    assert timeframe is Timeframe.DAY_1                # default timeframe
    assert (end - start).days == 183                   # default range = 6M
    body = r.json()
    assert body["period"] == 14                        # Wilder default
    # One candle can't warm a 14-period RSI: graceful empty, not an error.
    assert body["count"] == 0
    assert body["latest"] is None and body["signal"] is None


def test_get_rsi_honors_timeframe_range_and_period(make_client):
    fake = FakeCandleProvider(a_rising_series(timeframe=Timeframe.HOUR_1))
    client = make_client(rsi_provider=fake)
    r = client.get(
        "/stocks/AAPL/rsi",
        params={"timeframe": "1Hour", "range": "5D", "period": 3},
    )
    assert r.status_code == 200, r.text
    _, timeframe, start, end = fake.received[0]
    assert timeframe is Timeframe.HOUR_1
    assert (end - start).days == 5
    assert r.json()["period"] == 3


@pytest.mark.parametrize("bad_period", [1, 0, -5, 101])
def test_get_rsi_invalid_period_422(make_client, bad_period):
    client = make_client(rsi_provider=FakeCandleProvider(a_rising_series()))
    r = client.get("/stocks/AAPL/rsi", params={"period": bad_period})
    assert r.status_code == 422


def test_get_rsi_invalid_symbol_400(make_client):
    client = make_client(rsi_provider=FakeCandleProvider(a_rising_series()))
    assert client.get("/stocks/123/rsi").status_code == 400


def test_get_rsi_inverted_window_400(make_client):
    client = make_client(rsi_provider=FakeCandleProvider(a_rising_series()))
    r = client.get(
        "/stocks/AAPL/rsi",
        params={"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 400


def test_get_rsi_unknown_symbol_404(make_client):
    client = make_client(rsi_provider=FakeCandleProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ZZZZ/rsi").status_code == 404


def test_get_rsi_upstream_failure_502(make_client):
    fake = FakeCandleProvider(raises=StockDataUnavailable("AAPL", "boom"))
    client = make_client(rsi_provider=fake)
    assert client.get("/stocks/AAPL/rsi").status_code == 502


# --------------------------- earnings endpoint ---------------------------

def test_get_earnings_returns_200_with_beat_summary(make_client):
    history = a_history((
        a_surprise(actual=2.18, estimate=2.10, period=date(2026, 3, 31)),   # beat
        a_surprise(actual=1.40, estimate=1.50, period=date(2025, 12, 31)),  # miss
    ))
    client = make_client(earnings_provider=FakeEarningsProvider(history))
    r = client.get("/stocks/AAPL/earnings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["count"] == 2
    assert body["scored"] == 2
    assert body["beats"] == 1
    assert body["beat_rate"] == 50.0
    first = body["quarters"][0]
    assert first["period"] == "2026-03-31"   # date serialized ISO
    assert first["actual"] == 2.18
    assert first["beat"] is True
    assert body["quarters"][1]["beat"] is False


def test_get_earnings_includes_metrics_block_from_fundamentals(make_client):
    client = make_client(
        earnings_provider=FakeEarningsProvider(a_history()),
        fundamentals_provider=FakeFundamentalsProvider(a_fundamentals()),
    )
    body = client.get("/stocks/AAPL/earnings").json()
    metrics = body["metrics"]
    assert metrics["eps"] == 6.1
    assert metrics["net_margin"] == 25.0
    assert metrics["revenue_growth_yoy"] == 8.0
    assert metrics["payout_ratio"] == 15.0
    # valuation/market metrics belong to the stock endpoint, not here
    for stock_only in ("pe", "pb", "beta", "week_52_high"):
        assert stock_only not in metrics, stock_only


def test_get_earnings_metrics_null_without_fundamentals(make_client):
    client = make_client(earnings_provider=FakeEarningsProvider(a_history()))
    assert client.get("/stocks/AAPL/earnings").json()["metrics"] is None


def test_get_earnings_honors_limit(make_client):
    fake = FakeEarningsProvider(a_history())
    client = make_client(earnings_provider=fake)
    assert client.get("/stocks/AAPL/earnings", params={"limit": 12}).status_code == 200
    assert fake.received == [("AAPL", 12)]


def test_get_earnings_defaults_to_four_quarters(make_client):
    fake = FakeEarningsProvider(a_history())
    client = make_client(earnings_provider=fake)
    client.get("/stocks/AAPL/earnings")
    assert fake.received == [("AAPL", 4)]


def test_get_earnings_invalid_symbol_400(make_client):
    client = make_client(earnings_provider=FakeEarningsProvider(a_history()))
    assert client.get("/stocks/123/earnings").status_code == 400


def test_get_earnings_invalid_limit_422(make_client):
    client = make_client(earnings_provider=FakeEarningsProvider(a_history()))
    assert client.get("/stocks/AAPL/earnings", params={"limit": 0}).status_code == 422


def test_get_earnings_unknown_symbol_404(make_client):
    client = make_client(earnings_provider=FakeEarningsProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ZZZZ/earnings").status_code == 404


def test_get_earnings_upstream_failure_502(make_client):
    fake = FakeEarningsProvider(raises=StockDataUnavailable("AAPL", "boom"))
    client = make_client(earnings_provider=fake)
    assert client.get("/stocks/AAPL/earnings").status_code == 502


# --------------------------- sectors endpoint ---------------------------

def test_get_sectors_returns_200_ranked_with_computed_fields(make_client):
    tech = a_sector(sector="Technology", symbol="XLK", price=110.0, previous_close=100.0)
    energy = a_sector(sector="Energy", symbol="XLE", price=95.0, previous_close=100.0)
    client = make_client(sector_provider=FakeSectorProvider([energy, tech]))
    r = client.get("/sectors")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    first = body["sectors"][0]
    assert first["sector"] == "Technology"          # best performer first
    assert first["symbol"] == "XLK"
    assert first["change"] == 10.0                  # entity rule via presenter
    assert first["change_percent"] == 10.0
    assert body["sectors"][1]["change_percent"] == -5.0


def test_get_sectors_includes_trailing_performance_alias_keys(make_client):
    client = make_client(sector_provider=FakeSectorProvider([a_sector(performance=a_performance())]))
    body = client.get("/sectors").json()
    # trailing windows serialize with the finance-style JSON keys
    assert body["sectors"][0]["performance"] == {
        "1w": 1.2, "1m": -0.4, "3m": 5.1, "6m": 8.7, "ytd": 12.3, "1y": 21.0,
    }


def test_get_sectors_without_performance_is_null(make_client):
    client = make_client(sector_provider=FakeSectorProvider([a_sector()]))
    assert client.get("/sectors").json()["sectors"][0]["performance"] is None


def test_get_sectors_unknown_404(make_client):
    client = make_client(sector_provider=FakeSectorProvider(raises=StockNotFound("sectors")))
    assert client.get("/sectors").status_code == 404


def test_get_sectors_upstream_failure_502(make_client):
    fake = FakeSectorProvider(raises=StockDataUnavailable("sectors", "boom"))
    client = make_client(sector_provider=fake)
    assert client.get("/sectors").status_code == 502


# --------------------------- CORS ---------------------------

def test_cors_allows_configured_origin(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    origin = "https://namainsights.com"
    r = client.get("/stocks/AAPL", headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin


def test_cors_preflight_succeeds(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    r = client.options(
        "/stocks/AAPL",
        headers={
            "Origin": "https://namainsights.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200  # was 405 before CORSMiddleware
    assert r.headers["access-control-allow-origin"] == "https://namainsights.com"


# --------------------------- screener: fakes + builders ---------------------------

class FakeConstituentRepository(ConstituentRepository):
    """Returns a fixed universe; counts calls."""

    def __init__(self, constituents):
        self._constituents = tuple(constituents)
        self.calls = 0

    def all(self) -> tuple[Constituent, ...]:
        self.calls += 1
        return self._constituents


class FakeQuoteBatchProvider(QuoteBatchProvider):
    """Returns the configured quotes for whichever symbols are asked for.

    Mirrors the real best-effort contract: a requested symbol with no
    configured quote is simply omitted. Records the symbols it was asked for.
    """

    def __init__(self, quotes=None):
        self._quotes = dict(quotes or {})
        self.received: list[str] = []

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        self.received = list(symbols)
        return {s: self._quotes[s] for s in symbols if s in self._quotes}


def a_constituent(
    symbol="AAA", name=None, sector="Information Technology", indices=("sp500",)
) -> Constituent:
    return Constituent(
        symbol=symbol, name=name or symbol, sector=sector, indices=frozenset(indices)
    )


def a_universe() -> list[Constituent]:
    """Three names: two Information Technology (one also Nasdaq-100), one Energy."""
    return [
        a_constituent("AAA", sector="Information Technology", indices=("sp500",)),
        a_constituent(
            "BBB", sector="Information Technology", indices=("sp500", "nasdaq100")
        ),
        a_constituent("CCC", sector="Energy", indices=("sp500",)),
    ]


def movers_quotes(**moves) -> dict[str, Quote]:
    """Build symbol->Quote from keyword {symbol: (price, previous_close)} pairs."""
    return {
        symbol: a_quote(symbol=symbol, price=price, previous_close=prev)
        for symbol, (price, prev) in moves.items()
    }


# --------------------------- screener use case ---------------------------

def test_screen_ranks_gainers_desc_and_losers_worst_first():
    repo = FakeConstituentRepository(a_universe())
    quotes = FakeQuoteBatchProvider(
        movers_quotes(AAA=(110.0, 100.0), BBB=(90.0, 100.0), CCC=(105.0, 100.0))
    )
    board = ScreenStocks(repo, quotes).execute(limit=10)
    assert [s.symbol for s in board.gainers] == ["AAA", "CCC"]  # +10%, +5%
    assert [s.symbol for s in board.losers] == ["BBB"]          # the only negative
    assert board.gainers[0].change_percent == 10.0
    assert board.universe_count == 3
    assert board.quoted_count == 3


def test_screen_limit_caps_each_side_without_overlap():
    repo = FakeConstituentRepository(a_universe())
    quotes = FakeQuoteBatchProvider(
        movers_quotes(AAA=(110.0, 100.0), BBB=(90.0, 100.0), CCC=(105.0, 100.0))
    )
    board = ScreenStocks(repo, quotes).execute(limit=2)
    gainers = {s.symbol for s in board.gainers}
    losers = {s.symbol for s in board.losers}
    assert gainers == {"AAA", "CCC"}   # top 2 by gain
    assert losers == {"BBB"}           # worst, and never also a gainer
    assert gainers.isdisjoint(losers)  # a symbol is never in both


def test_screen_filters_by_index():
    repo = FakeConstituentRepository(a_universe())
    quotes = FakeQuoteBatchProvider(
        movers_quotes(AAA=(110.0, 100.0), BBB=(90.0, 100.0), CCC=(105.0, 100.0))
    )
    board = ScreenStocks(repo, quotes).execute(index=StockIndex.NASDAQ100)
    assert board.universe_count == 1
    assert quotes.received == ["BBB"]  # only the Nasdaq-100 name is fetched


def test_screen_filters_by_sector_case_insensitively():
    repo = FakeConstituentRepository(a_universe())
    quotes = FakeQuoteBatchProvider(movers_quotes(CCC=(105.0, 100.0)))
    board = ScreenStocks(repo, quotes).execute(sector="energy")
    assert board.universe_count == 1
    assert [s.symbol for s in board.gainers] == ["CCC"]


def test_screen_excludes_names_without_a_usable_quote():
    repo = FakeConstituentRepository(a_universe())
    # BBB has no quote at all; CCC has one but no previous close (no percent).
    quotes = FakeQuoteBatchProvider({
        **movers_quotes(AAA=(110.0, 100.0)),
        "CCC": a_quote(symbol="CCC", previous_close=None),
    })
    board = ScreenStocks(repo, quotes).execute()
    assert board.universe_count == 3  # the filter matched all three
    assert board.quoted_count == 1    # only AAA could actually be ranked
    assert [s.symbol for s in board.gainers] == ["AAA"]


def test_screen_empty_universe_returns_empty_board():
    repo = FakeConstituentRepository(a_universe())
    quotes = FakeQuoteBatchProvider()
    board = ScreenStocks(repo, quotes).execute(sector="Nonexistent Sector")
    assert board.universe_count == 0
    assert board.gainers == () and board.losers == ()
    assert quotes.received == []  # nothing to fetch


def test_screen_no_quotes_for_nonempty_universe_is_unavailable():
    # The feed returned nothing for a real universe -> upstream is down, not a
    # genuinely flat market. Surface it rather than serving an empty board.
    repo = FakeConstituentRepository(a_universe())
    with pytest.raises(StockDataUnavailable):
        ScreenStocks(repo, FakeQuoteBatchProvider()).execute()


def test_screen_rejects_non_positive_limit():
    repo = FakeConstituentRepository(a_universe())
    with pytest.raises(ValueError):
        ScreenStocks(repo, FakeQuoteBatchProvider()).execute(limit=0)


def test_screen_as_of_is_latest_quote_timestamp():
    repo = FakeConstituentRepository(a_universe())
    early = datetime(2026, 6, 18, 15, 0, tzinfo=timezone.utc)
    late = datetime(2026, 6, 18, 20, 0, tzinfo=timezone.utc)
    quotes = FakeQuoteBatchProvider({
        "AAA": a_quote(symbol="AAA", price=110.0, previous_close=100.0, as_of=early),
        "BBB": a_quote(symbol="BBB", price=90.0, previous_close=100.0, as_of=late),
    })
    assert ScreenStocks(repo, quotes).execute().as_of == late


# --------------------------- screener endpoint ---------------------------

@pytest.fixture
def make_screener_client():
    def _make(use_case: ScreenStocks) -> TestClient:
        app.dependency_overrides[get_screener] = lambda: use_case
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def a_screener(quotes=None, constituents=None) -> ScreenStocks:
    default = movers_quotes(AAA=(110.0, 100.0), BBB=(90.0, 100.0), CCC=(105.0, 100.0))
    return ScreenStocks(
        FakeConstituentRepository(constituents or a_universe()),
        FakeQuoteBatchProvider(default if quotes is None else quotes),
    )


def test_get_screener_returns_200_with_movers(make_screener_client):
    client = make_screener_client(a_screener())
    r = client.get("/stocks/screener")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [g["symbol"] for g in body["gainers"]] == ["AAA", "CCC"]
    assert body["gainers"][0]["change_percent"] == 10.0  # entity rule via presenter
    assert [l["symbol"] for l in body["losers"]] == ["BBB"]
    assert body["losers"][0]["change_percent"] == -10.0
    assert body["universe_count"] == 3 and body["quoted_count"] == 3
    assert body["gainers"][0]["sector"] == "Information Technology"


def test_get_screener_sets_short_cache_header(make_screener_client):
    client = make_screener_client(a_screener())
    r = client.get("/stocks/screener")
    assert r.headers["cache-control"] == "public, max-age=15"


def test_get_screener_is_not_captured_by_symbol_route(make_screener_client):
    # "/stocks/screener" must hit the screener, not "/stocks/{symbol}" with
    # symbol="screener" (which would 400 as an invalid symbol).
    client = make_screener_client(a_screener())
    assert client.get("/stocks/screener").status_code == 200


def test_get_screener_honors_limit(make_screener_client):
    client = make_screener_client(a_screener())
    body = client.get("/stocks/screener", params={"limit": 1}).json()
    assert len(body["gainers"]) == 1 and len(body["losers"]) == 1
    assert body["limit"] == 1


def test_get_screener_filters_by_index_and_sector(make_screener_client):
    client = make_screener_client(a_screener())
    ndx = client.get("/stocks/screener", params={"index": "nasdaq100"}).json()
    assert ndx["index"] == "nasdaq100"
    assert [g["symbol"] for g in ndx["gainers"]] == ["BBB"]
    energy = client.get("/stocks/screener", params={"sector": "Energy"}).json()
    assert energy["universe_count"] == 1


@pytest.mark.parametrize("bad", [0, 51, -1])
def test_get_screener_invalid_limit_422(make_screener_client, bad):
    client = make_screener_client(a_screener())
    assert client.get("/stocks/screener", params={"limit": bad}).status_code == 422


def test_get_screener_invalid_index_422(make_screener_client):
    client = make_screener_client(a_screener())
    assert client.get("/stocks/screener", params={"index": "dow"}).status_code == 422


def test_get_screener_empty_universe_returns_empty_lists(make_screener_client):
    client = make_screener_client(a_screener())
    body = client.get("/stocks/screener", params={"sector": "Nope"}).json()
    assert body["universe_count"] == 0
    assert body["gainers"] == [] and body["losers"] == []


def test_get_screener_upstream_failure_502(make_screener_client):
    # Real universe, but the quote feed yields nothing -> 502.
    client = make_screener_client(a_screener(quotes={}))
    assert client.get("/stocks/screener").status_code == 502
