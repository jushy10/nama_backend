"""Unit tests for the Alpaca adapter.

No network: the real Alpaca clients are swapped for fakes, and the pure
mapping is tested directly. Verifies an adapter's two jobs — translate
Alpaca models -> Stock entity, and Alpaca failures -> domain exceptions.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed

from app.stocks.alpaca_provider import AlpacaStockDataProvider
from app.stocks.entities import Candle, Quote, Stock, StockPerformance, Timeframe
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import (
    CandleProvider,
    QuoteBatchProvider,
    SectorPerformanceProvider,
    StockDataProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)


def make_snapshot():
    return SimpleNamespace(
        latest_trade=SimpleNamespace(
            price=297.86, timestamp=datetime(2026, 6, 18, tzinfo=timezone.utc)
        ),
        latest_quote=SimpleNamespace(bid_price=283.52, ask_price=313.43),
        daily_bar=SimpleNamespace(
            open=298.44, high=300.56, low=295.635, close=297.86, volume=1278873.0
        ),
        previous_daily_bar=SimpleNamespace(close=296.07),
        minute_bar=None,
    )


class FakeDataClient:
    def __init__(self, result=None, error=None, bars=None, bars_error=None):
        self._result, self._error = result, error
        self._bars, self._bars_error = bars, bars_error
        self.last_bars_request = None  # captured for request-shape assertions

    def get_stock_snapshot(self, request):
        if self._error is not None:
            raise self._error
        return self._result

    def get_stock_bars(self, request):
        self.last_bars_request = request
        if self._bars_error is not None:
            raise self._bars_error
        return SimpleNamespace(data=self._bars or {})


class FakeTradingClient:
    def __init__(self, asset=None, error=None):
        self._asset, self._error = asset, error

    def get_asset(self, symbol):
        if self._error is not None:
            raise self._error
        return self._asset


class ExplodingTradingClient:
    """Fails if touched — proves get_quote never makes the asset-metadata call."""

    def get_asset(self, symbol):
        raise AssertionError("get_quote must not call the trading client")


def provider_with(data_client, trading_client) -> AlpacaStockDataProvider:
    # Construction is offline (clients only store credentials); then swap the
    # real clients for fakes so get_stock() makes no network calls.
    p = AlpacaStockDataProvider("dummy-key", "dummy-secret")
    p._data = data_client
    p._trading = trading_client
    return p


def test_to_entity_maps_every_field():
    stock = AlpacaStockDataProvider._to_entity(
        "AAPL", make_snapshot(), "Apple Inc.", "NASDAQ"
    )
    assert isinstance(stock, Stock)
    assert stock.symbol == "AAPL"
    assert stock.price == 297.86
    assert stock.previous_close == 296.07
    assert stock.volume == 1278873  # float -> int
    assert stock.bid == 283.52 and stock.ask == 313.43
    assert stock.change == 1.79


def test_get_stock_happy_path():
    asset = SimpleNamespace(name="Apple Inc.", exchange=SimpleNamespace(value="NASDAQ"))
    p = provider_with(
        FakeDataClient(result={"AAPL": make_snapshot()}),
        FakeTradingClient(asset=asset),
    )
    stock = p.get_stock("AAPL")
    assert stock.name == "Apple Inc."
    assert stock.exchange == "NASDAQ"
    assert stock.price == 297.86


def test_missing_snapshot_raises_not_found():
    p = provider_with(FakeDataClient(result={"AAPL": None}), FakeTradingClient())
    with pytest.raises(StockNotFound):
        p.get_stock("AAPL")


def test_snapshot_without_trade_raises_not_found():
    snap = make_snapshot()
    snap.latest_trade = None
    p = provider_with(FakeDataClient(result={"AAPL": snap}), FakeTradingClient())
    with pytest.raises(StockNotFound):
        p.get_stock("AAPL")


def test_api_error_translated_to_unavailable():
    p = provider_with(FakeDataClient(error=APIError("boom")), FakeTradingClient())
    with pytest.raises(StockDataUnavailable):
        p.get_stock("AAPL")


def test_asset_metadata_failure_is_non_fatal():
    p = provider_with(
        FakeDataClient(result={"AAPL": make_snapshot()}),
        FakeTradingClient(error=APIError("no asset")),
    )
    stock = p.get_stock("AAPL")
    assert stock.name is None
    assert stock.exchange is None
    assert stock.price == 297.86  # market data still returned


# --------------------------- quote (snapshot only) ---------------------------


def test_to_quote_maps_snapshot_fields():
    quote = AlpacaStockDataProvider._to_quote("AAPL", make_snapshot())
    assert isinstance(quote, Quote)
    assert quote.symbol == "AAPL"
    assert quote.price == 297.86
    assert quote.previous_close == 296.07
    assert quote.bid == 283.52 and quote.ask == 313.43
    assert quote.change == 1.79  # entity rule, same as Stock's
    assert quote.as_of == datetime(2026, 6, 18, tzinfo=timezone.utc)


def test_get_quote_happy_path():
    p = provider_with(
        FakeDataClient(result={"AAPL": make_snapshot()}), FakeTradingClient()
    )
    quote = p.get_quote("AAPL")
    assert quote.price == 297.86
    assert quote.change_percent == 0.6


def test_get_quote_skips_trading_client():
    # The whole point of the slim endpoint: one snapshot call, no asset lookup.
    p = provider_with(
        FakeDataClient(result={"AAPL": make_snapshot()}), ExplodingTradingClient()
    )
    assert p.get_quote("AAPL").price == 297.86  # would raise if trading touched


def test_get_quote_missing_snapshot_raises_not_found():
    p = provider_with(FakeDataClient(result={"AAPL": None}), FakeTradingClient())
    with pytest.raises(StockNotFound):
        p.get_quote("AAPL")


def test_get_quote_api_error_translated_to_unavailable():
    p = provider_with(FakeDataClient(error=APIError("boom")), FakeTradingClient())
    with pytest.raises(StockDataUnavailable):
        p.get_quote("AAPL")


# --------------------------- performance from bars ---------------------------


def bar(year, month, day, close):
    return SimpleNamespace(
        timestamp=datetime(year, month, day, tzinfo=timezone.utc), close=close
    )


def performance_bars():
    """Bars placed exactly on each window's start date; current close = 110."""
    return [
        bar(2025, 6, 18, 55.0),  # 1y  -> +100%
        bar(2025, 12, 18, 50.0),  # 6m  -> +120%
        bar(2025, 12, 31, 100.0),  # ytd baseline (last 2025 close) -> +10%
        bar(2026, 3, 19, 80.0),  # 3m  -> +37.5%
        bar(2026, 5, 19, 88.0),  # 1m  -> +25%
        bar(2026, 6, 11, 100.0),  # 1w  -> +10%
        bar(2026, 6, 18, 110.0),  # anchor / current price
    ]


def test_compute_performance_maps_each_window():
    # Reversed input also exercises the defensive sort.
    perf = AlpacaStockDataProvider._compute_performance(
        list(reversed(performance_bars()))
    )
    assert perf.one_week == 10.0
    assert perf.one_month == 25.0
    assert perf.three_month == 37.5
    assert perf.six_month == 120.0
    assert perf.ytd == 10.0
    assert perf.one_year == 100.0


def test_compute_performance_empty_bars_all_none():
    assert AlpacaStockDataProvider._compute_performance([]) == StockPerformance(
        None, None, None, None, None, None
    )


def test_compute_performance_insufficient_history_yields_none():
    # Only two recent bars: 1w computes; longer windows and YTD are None.
    bars = [bar(2026, 6, 11, 100.0), bar(2026, 6, 18, 110.0)]
    perf = AlpacaStockDataProvider._compute_performance(bars)
    assert perf.one_week == 10.0
    assert perf.one_month is None
    assert perf.one_year is None
    assert perf.ytd is None  # no bar from a previous year


def test_get_performance_reads_bars_from_client():
    p = provider_with(
        FakeDataClient(bars={"AAPL": performance_bars()}), FakeTradingClient()
    )
    perf = p.get_performance("AAPL")
    assert perf.one_year == 100.0
    assert perf.six_month == 120.0


def test_performance_bars_read_consolidated_split_adjusted_feed():
    # Trailing returns must anchor on the real consolidated close, not IEX's
    # single-venue print, and survive splits. The free plan permits SIP for
    # history only when the query ends >15 min in the past, so `end` is held back.
    client = FakeDataClient(bars={"AAPL": performance_bars()})
    p = provider_with(client, FakeTradingClient())
    p.get_performance("AAPL")
    req = client.last_bars_request
    assert req.feed == DataFeed.SIP
    assert req.adjustment == Adjustment.SPLIT
    # `end` held back from "now" (SIP-on-free needs >15 min old) and after start.
    assert req.end is not None and req.start < req.end


def test_get_performance_api_error_translated_to_unavailable():
    p = provider_with(FakeDataClient(bars_error=APIError("boom")), FakeTradingClient())
    with pytest.raises(StockDataUnavailable):
        p.get_performance("AAPL")


def test_get_performance_unknown_symbol_returns_empty():
    # No series for the symbol -> all-None performance (not an error).
    p = provider_with(FakeDataClient(bars={}), FakeTradingClient())
    assert p.get_performance("AAPL") == StockPerformance(
        None, None, None, None, None, None
    )


# --------------------------- sector performance ---------------------------


def test_get_sector_performance_attaches_day_change_and_windows():
    # One snapshot batch + one bars batch. XLK has bars; XLV has a snapshot but
    # no bars, so it still appears with all-None trailing windows.
    p = provider_with(
        FakeDataClient(
            result={"XLK": make_snapshot(), "XLV": make_snapshot()},
            bars={"XLK": performance_bars()},
        ),
        FakeTradingClient(),
    )
    by_symbol = {s.symbol: s for s in p.get_sector_performance()}
    assert by_symbol["XLK"].change == 1.79  # day change from the snapshot
    assert by_symbol["XLK"].performance.one_year == 100.0  # windows from bars
    assert by_symbol["XLK"].performance.six_month == 120.0
    assert by_symbol["XLV"].performance.one_year is None  # no bars -> None


def test_get_sector_performance_bars_failure_keeps_day_change():
    # Performance is best-effort: a bars failure must not sink the board.
    p = provider_with(
        FakeDataClient(result={"XLK": make_snapshot()}, bars_error=APIError("boom")),
        FakeTradingClient(),
    )
    sectors = p.get_sector_performance()
    assert sectors[0].change == 1.79
    assert sectors[0].performance.one_year is None


def test_get_sector_performance_snapshot_error_unavailable():
    p = provider_with(FakeDataClient(error=APIError("boom")), FakeTradingClient())
    with pytest.raises(StockDataUnavailable):
        p.get_sector_performance()


def test_get_sector_performance_empty_board_not_found():
    p = provider_with(FakeDataClient(result={}), FakeTradingClient())
    with pytest.raises(StockNotFound):
        p.get_sector_performance()


# --------------------------- candles ---------------------------

def make_bar(ts, open_, high, low, close, volume=1000.0):
    return SimpleNamespace(
        timestamp=ts, open=open_, high=high, low=low, close=close, volume=volume
    )


class FakeBarsClient:
    """Stands in for StockHistoricalDataClient.get_stock_bars."""

    def __init__(self, bars_by_symbol=None, error=None):
        self._barset = SimpleNamespace(data=bars_by_symbol or {})
        self._error = error
        self.last_request = None

    def get_stock_bars(self, request):
        self.last_request = request
        if self._error is not None:
            raise self._error
        return self._barset


def bars_provider(client) -> AlpacaStockDataProvider:
    p = AlpacaStockDataProvider("dummy-key", "dummy-secret")
    p._data = client
    return p


def test_to_candle_maps_fields_and_casts_volume():
    bar = make_bar(datetime(2026, 6, 18, tzinfo=timezone.utc), 100.0, 105.0, 99.0, 104.0)
    candle = AlpacaStockDataProvider._to_candle(bar)
    assert isinstance(candle, Candle)
    assert (candle.open, candle.high, candle.low, candle.close) == (100.0, 105.0, 99.0, 104.0)
    assert candle.volume == 1000  # float -> int
    assert candle.is_bullish is True


def test_get_candles_returns_chronological_order():
    # Alpaca is asked for newest-first (sort=DESC); the adapter must reverse it.
    newest = make_bar(datetime(2026, 6, 19, tzinfo=timezone.utc), 110, 111, 108, 109)
    oldest = make_bar(datetime(2026, 6, 18, tzinfo=timezone.utc), 100, 106, 99, 105)
    p = bars_provider(FakeBarsClient(bars_by_symbol={"AAPL": [newest, oldest]}))
    series = p.get_candles("AAPL", Timeframe.DAY_1, start=None, end=None)
    times = [c.timestamp for c in series.candles]
    assert times == sorted(times)  # oldest first
    assert series.timeframe is Timeframe.DAY_1


def test_get_candles_empty_raises_not_found():
    p = bars_provider(FakeBarsClient(bars_by_symbol={}))
    with pytest.raises(StockNotFound):
        p.get_candles("ZZZZ", Timeframe.DAY_1, start=None, end=None)


def test_get_candles_api_error_translated_to_unavailable():
    p = bars_provider(FakeBarsClient(error=APIError("boom")))
    with pytest.raises(StockDataUnavailable):
        p.get_candles("AAPL", Timeframe.HOUR_1, start=None, end=None)


# --------------------------- port composition ---------------------------


def test_provider_implements_all_ports():
    # The merged adapter serves the snapshot, performance, candle, and sector
    # ports from one instance. The router relies on this: get_stock_info uses an
    # isinstance check to reuse the provider as the StockPerformanceProvider, so
    # a dropped base class would silently stop a feature from populating.
    p = AlpacaStockDataProvider("dummy-key", "dummy-secret")
    assert isinstance(p, StockDataProvider)
    assert isinstance(p, StockQuoteProvider)
    assert isinstance(p, QuoteBatchProvider)
    assert isinstance(p, StockPerformanceProvider)
    assert isinstance(p, CandleProvider)
    assert isinstance(p, SectorPerformanceProvider)


# --------------------------- batch quotes (screener) ---------------------------


class RecordingSnapshotClient:
    """Returns a snapshot for every requested symbol and records each request's
    symbol list, so chunking can be asserted."""

    def __init__(self):
        self.requests: list[list[str]] = []

    def get_stock_snapshot(self, request):
        symbols = list(request.symbol_or_symbols)
        self.requests.append(symbols)
        return {symbol: make_snapshot() for symbol in symbols}


def test_get_quotes_maps_each_requested_symbol():
    p = provider_with(
        FakeDataClient(result={"AAPL": make_snapshot(), "MSFT": make_snapshot()}),
        FakeTradingClient(),
    )
    quotes = p.get_quotes(["AAPL", "MSFT"])
    assert set(quotes) == {"AAPL", "MSFT"}
    assert all(isinstance(q, Quote) for q in quotes.values())
    assert quotes["AAPL"].change_percent == 0.6  # same rule as get_quote


def test_get_quotes_omits_symbols_without_a_trade():
    no_trade = make_snapshot()
    no_trade.latest_trade = None
    p = provider_with(
        FakeDataClient(result={"AAPL": make_snapshot(), "MSFT": no_trade, "ZZZZ": None}),
        FakeTradingClient(),
    )
    # The tradeless and the missing names drop out; only the priced one survives.
    assert set(p.get_quotes(["AAPL", "MSFT", "ZZZZ"])) == {"AAPL"}


def test_get_quotes_chunk_failure_is_best_effort():
    # An API error yields an empty map rather than raising — the screener
    # decides what an empty result means.
    p = provider_with(FakeDataClient(error=APIError("boom")), FakeTradingClient())
    assert p.get_quotes(["AAPL", "MSFT"]) == {}


def test_get_quotes_batches_in_chunks_of_200():
    client = RecordingSnapshotClient()
    p = AlpacaStockDataProvider("dummy-key", "dummy-secret")
    p._data = client
    symbols = [f"S{i}" for i in range(250)]
    quotes = p.get_quotes(symbols)
    assert len(quotes) == 250
    # 250 symbols at 200/chunk -> two requests (200 + 50).
    assert [len(req) for req in client.requests] == [200, 50]
