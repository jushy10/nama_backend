"""Tests for the stocks vertical slice: entity rules, use case, and the API.

Everything here runs offline. The use case depends on the StockDataProvider
port, so we inject a hand-written FakeProvider instead of mocking Alpaca or
the network — that's the payoff of the clean-architecture layering.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.stocks.chart_window import ChartRange, resolve_window
from app.stocks.entities import (
    Candle,
    CandleSeries,
    KeyMetrics,
    Logo,
    Stock,
    StockFundamentals,
    StockPerformance,
    Timeframe,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.indicators import RsiSignal
from app.stocks.ports import (
    CandleProvider,
    LogoProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
)
from app.stocks.router import (
    get_stock_candles,
    get_stock_info,
    get_stock_logo,
    get_stock_rsi,
)
from app.stocks.use_cases import (
    GetStockCandles,
    GetStockInfo,
    GetStockLogo,
    GetStockRsi,
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


def a_performance(**overrides) -> StockPerformance:
    base = dict(
        one_week=1.2, one_month=-0.4, three_month=5.1,
        six_month=8.7, ytd=12.3, one_year=21.0,
    )
    base.update(overrides)
    return StockPerformance(**base)


def a_key_metrics(**overrides) -> KeyMetrics:
    base = dict(
        pe=28.5, pb=45.2, ps=7.1, eps=6.1, roe=150.0, beta=1.2,
        week_52_high=320.0, week_52_low=210.0,
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
    )
    stock = info.execute("AAPL")
    assert stock.market_cap == 3_120_000_000_000.0
    assert stock.dividend_per_share == 1.0
    assert stock.dividend_yield == 0.42
    assert stock.performance.one_year == 21.0
    assert stock.metrics.pe == 28.5
    assert stock.metrics.beta == 1.2


def test_use_case_without_enrichment_leaves_fields_none():
    stock = GetStockInfo(FakeProvider(stock=a_stock())).execute("AAPL")
    assert stock.market_cap is None
    assert stock.dividend_yield is None
    assert stock.performance is None
    assert stock.metrics is None


def test_use_case_enrichment_is_best_effort():
    info = GetStockInfo(
        FakeProvider(stock=a_stock()),
        FakePerformanceProvider(raises=StockDataUnavailable("AAPL", "boom")),
        FakeFundamentalsProvider(raises=StockNotFound("AAPL")),
    )
    stock = info.execute("AAPL")  # enrichment failures must not raise
    assert stock.price == 297.86
    assert stock.performance is None
    assert stock.market_cap is None


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


# --------------------------- API ---------------------------

@pytest.fixture
def make_client():
    def _make(
        provider: StockDataProvider | None = None,
        logo_provider: LogoProvider | None = None,
        performance_provider: StockPerformanceProvider | None = None,
        fundamentals_provider: StockFundamentalsProvider | None = None,
        candle_provider: CandleProvider | None = None,
        rsi_provider: CandleProvider | None = None,
    ) -> TestClient:
        if provider is not None:
            app.dependency_overrides[get_stock_info] = lambda: GetStockInfo(
                provider, performance_provider, fundamentals_provider
            )
        if logo_provider is not None:
            app.dependency_overrides[get_stock_logo] = lambda: GetStockLogo(logo_provider)
        if candle_provider is not None:
            app.dependency_overrides[get_stock_candles] = (
                lambda: GetStockCandles(candle_provider)
            )
        if rsi_provider is not None:
            app.dependency_overrides[get_stock_rsi] = lambda: GetStockRsi(rsi_provider)
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
    )
    body = client.get("/stocks/AAPL").json()
    assert body["market_cap"] == 3_120_000_000_000.0
    assert body["dividend_per_share"] == 1.0
    assert body["dividend_yield"] == 0.42
    # nested performance is serialized with finance-style JSON keys
    assert body["performance"] == {
        "1w": 1.2, "1m": -0.4, "3m": 5.1, "6m": 8.7, "ytd": 12.3, "1y": 21.0,
    }
    # nested key metrics ride along on the same fundamentals payload
    assert body["metrics"]["pe"] == 28.5
    assert body["metrics"]["beta"] == 1.2
    assert body["metrics"]["week_52_high"] == 320.0
    assert body["metrics"]["ps"] == 7.1


def test_get_stock_enrichment_best_effort_returns_200(make_client):
    client = make_client(
        FakeProvider(stock=a_stock()),
        performance_provider=FakePerformanceProvider(
            raises=StockDataUnavailable("AAPL", "boom")
        ),
        fundamentals_provider=FakeFundamentalsProvider(raises=StockNotFound("AAPL")),
    )
    r = client.get("/stocks/AAPL")
    assert r.status_code == 200, r.text  # price survives enrichment failures
    body = r.json()
    assert body["price"] == 297.86
    assert body["market_cap"] is None
    assert body["performance"] is None


def test_get_stock_without_enrichment_providers_nulls_fields(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    body = client.get("/stocks/AAPL").json()
    assert body["market_cap"] is None
    assert body["dividend_per_share"] is None
    assert body["performance"] is None
    assert body["metrics"] is None


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
