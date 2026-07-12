"""Unit tests for the Alpaca adapter.

No network: the real Alpaca clients are swapped for fakes, and the pure
mapping is tested directly. Verifies an adapter's two jobs — translate
Alpaca models -> Stock entity, and Alpaca failures -> domain exceptions.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed

from app.stocks.adapters.alpaca_adapter import AlpacaStockDataProvider
from app.stocks.charts.ports import CandleProvider
from app.stocks.entities import (
    AllTimeHigh,
    Candle,
    Quote,
    Stock,
    StockPerformance,
    Timeframe,
)
from app.stocks.market.ports import MarketOverviewProvider, SectorPerformanceProvider
from app.stocks.ports import (
    AllTimeHighProvider,
    BulkQuoteProvider,
    StockDataProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


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


# --------------------------- market overview ---------------------------


def test_get_market_overview_attaches_day_change_and_windows():
    # One snapshot batch + one bars batch. SPY has bars; QQQ has a snapshot but
    # no bars, so it still appears with all-None trailing windows.
    p = provider_with(
        FakeDataClient(
            result={"SPY": make_snapshot(), "QQQ": make_snapshot()},
            bars={"SPY": performance_bars()},
        ),
        FakeTradingClient(),
    )
    by_symbol = {i.symbol: i for i in p.get_market_overview()}
    assert by_symbol["SPY"].name == "S&P 500"
    assert by_symbol["QQQ"].name == "Nasdaq"
    assert by_symbol["SPY"].change == 1.79  # day change from the snapshot
    assert by_symbol["SPY"].performance.one_year == 100.0  # windows from bars
    assert by_symbol["QQQ"].performance.one_year is None  # no bars -> None


def test_get_market_overview_bars_failure_keeps_day_change():
    # Performance is best-effort: a bars failure must not sink the board.
    p = provider_with(
        FakeDataClient(result={"SPY": make_snapshot()}, bars_error=APIError("boom")),
        FakeTradingClient(),
    )
    indexes = p.get_market_overview()
    assert indexes[0].change == 1.79
    assert indexes[0].performance.one_year is None


def test_get_market_overview_snapshot_error_unavailable():
    p = provider_with(FakeDataClient(error=APIError("boom")), FakeTradingClient())
    with pytest.raises(StockDataUnavailable):
        p.get_market_overview()


def test_get_market_overview_empty_board_not_found():
    p = provider_with(FakeDataClient(result={}), FakeTradingClient())
    with pytest.raises(StockNotFound):
        p.get_market_overview()


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


def test_to_candle_clamps_bad_low_spike():
    # The real incident: SPY's 2026-02-02 daily bar came back low=69 against a
    # ~690 body — a corrupt tick that drew a giant downward wick. The low is
    # pulled up to the body bottom; open/high/close/volume pass through.
    bar = make_bar(datetime(2026, 2, 2, tzinfo=timezone.utc), 689.58, 696.93, 69.0, 695.41)
    candle = AlpacaStockDataProvider._to_candle(bar)
    assert candle.low == 689.58  # min(open, close) — the spike removed
    assert (candle.open, candle.high, candle.close) == (689.58, 696.93, 695.41)


def test_to_candle_clamps_bad_high_spike():
    # Symmetric guard: a garbage high far above the body is pulled back in,
    # while a plausible lower wick on the same bar is left alone.
    bar = make_bar(datetime(2026, 2, 2, tzinfo=timezone.utc), 100.0, 6900.0, 98.0, 104.0)
    candle = AlpacaStockDataProvider._to_candle(bar)
    assert candle.high == 104.0  # max(open, close) — the spike removed
    assert candle.low == 98.0  # plausible wick untouched


def test_to_candle_keeps_steep_but_plausible_wick():
    # A steep-but-real intraday wick (~-30% here) stays: only spikes past the
    # body fraction are treated as corrupt, so genuine moves pass through.
    bar = make_bar(datetime(2026, 6, 18, tzinfo=timezone.utc), 100.0, 108.0, 70.0, 104.0)
    candle = AlpacaStockDataProvider._to_candle(bar)
    assert (candle.high, candle.low) == (108.0, 70.0)


def test_get_candles_returns_chronological_order():
    # Alpaca is asked for newest-first (sort=DESC); the adapter must reverse it.
    newest = make_bar(datetime(2026, 6, 19, tzinfo=timezone.utc), 110, 111, 108, 109)
    oldest = make_bar(datetime(2026, 6, 18, tzinfo=timezone.utc), 100, 106, 99, 105)
    p = bars_provider(FakeBarsClient(bars_by_symbol={"AAPL": [newest, oldest]}))
    series = p.get_candles("AAPL", Timeframe.DAY_1, start=None, end=None)
    times = [c.timestamp for c in series.candles]
    assert times == sorted(times)  # oldest first
    assert series.timeframe is Timeframe.DAY_1


def _aware(dt):
    """Alpaca's request model stores datetimes tz-naive (UTC); re-attach it."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def test_get_candles_daily_reads_consolidated_feed_with_delayed_end():
    # Long-range charts (daily/weekly/monthly) must read SIP: IEX's history is
    # gappy and, on the free plan, only reaches ~mid-2020, so a 10Y window comes
    # up short. SIP-on-free requires the query to end >15 min in the past, so
    # `end` is held back from now even when the caller passes none.
    client = FakeBarsClient(
        bars_by_symbol={"AAPL": [make_bar(datetime(2016, 1, 4, tzinfo=timezone.utc), 90, 100, 88, 95)]}
    )
    p = bars_provider(client)
    p.get_candles("AAPL", Timeframe.WEEK_1, start=None, end=None)
    req = client.last_request
    assert req.feed == DataFeed.SIP
    assert req.adjustment == Adjustment.SPLIT
    assert req.end is not None
    assert _aware(req.end) <= datetime.now(timezone.utc) - timedelta(minutes=15)


def test_get_candles_intraday_reads_realtime_feed_without_delay():
    # Intraday charts (1D/7D/1M) need real-time IEX prints, and the window is
    # recent enough that IEX carries it — so the feed stays IEX and `end` passes
    # through untouched (no SIP-history hold-back).
    client = FakeBarsClient(
        bars_by_symbol={"AAPL": [make_bar(datetime(2026, 6, 19, 15, tzinfo=timezone.utc), 1, 2, 1, 2)]}
    )
    p = bars_provider(client)
    end = datetime(2026, 6, 19, 20, tzinfo=timezone.utc)
    p.get_candles("AAPL", Timeframe.HOUR_4, start=None, end=end)
    req = client.last_request
    assert req.feed == DataFeed.IEX
    assert _aware(req.end) == end  # passed through, not clamped


def test_get_candles_empty_raises_not_found():
    p = bars_provider(FakeBarsClient(bars_by_symbol={}))
    with pytest.raises(StockNotFound):
        p.get_candles("ZZZZ", Timeframe.DAY_1, start=None, end=None)


def test_get_candles_api_error_translated_to_unavailable():
    p = bars_provider(FakeBarsClient(error=APIError("boom")))
    with pytest.raises(StockDataUnavailable):
        p.get_candles("AAPL", Timeframe.HOUR_1, start=None, end=None)


# --------------------------- all-time high ---------------------------


def ath_bars():
    """Daily history whose peak intraday high (130) prints on 2024-03-04, with the
    earliest bar in 2016 — the bound the high is computed over."""
    return [
        make_bar(datetime(2016, 1, 4, tzinfo=timezone.utc), 90, 100, 88, 95),
        make_bar(datetime(2024, 3, 4, tzinfo=timezone.utc), 120, 130, 118, 125),  # peak
        make_bar(datetime(2026, 6, 18, tzinfo=timezone.utc), 110, 115, 108, 112),
    ]


def test_to_all_time_high_picks_peak_high_and_bounds():
    high = AlpacaStockDataProvider._to_all_time_high(ath_bars())
    assert isinstance(high, AllTimeHigh)
    assert high.price == 130                    # the highest intraday high...
    assert high.reached_on == date(2024, 3, 4)  # ...and the day it printed
    assert high.since == date(2016, 1, 4)       # earliest bar = the history bound


def test_get_all_time_high_reads_bars_from_client():
    p = bars_provider(FakeBarsClient(bars_by_symbol={"AAPL": ath_bars()}))
    high = p.get_all_time_high("AAPL")
    assert high.price == 130
    assert high.since == date(2016, 1, 4)


def test_all_time_high_reads_consolidated_split_adjusted_feed():
    # Same rationale as performance: the all-time high must come from the full
    # consolidated, split-adjusted history, not IEX's gappy single-venue print.
    client = FakeBarsClient(bars_by_symbol={"AAPL": ath_bars()})
    p = bars_provider(client)
    p.get_all_time_high("AAPL")
    req = client.last_request
    assert req.feed == DataFeed.SIP
    assert req.adjustment == Adjustment.SPLIT
    # `start` reaches back past the data floor; `end` is held back from now.
    assert req.end is not None and req.start < req.end


def test_get_all_time_high_empty_raises_not_found():
    p = bars_provider(FakeBarsClient(bars_by_symbol={}))
    with pytest.raises(StockNotFound):
        p.get_all_time_high("ZZZZ")


def test_get_all_time_high_api_error_translated_to_unavailable():
    p = bars_provider(FakeBarsClient(error=APIError("boom")))
    with pytest.raises(StockDataUnavailable):
        p.get_all_time_high("AAPL")


# --------------------------- port composition ---------------------------


def test_provider_implements_all_ports():
    # The merged adapter serves the snapshot, performance, candle, and sector
    # ports from one instance. The router relies on this: get_stock_info uses an
    # isinstance check to reuse the provider as the StockPerformanceProvider, so
    # a dropped base class would silently stop a feature from populating.
    p = AlpacaStockDataProvider("dummy-key", "dummy-secret")
    assert isinstance(p, StockDataProvider)
    assert isinstance(p, StockQuoteProvider)
    assert isinstance(p, BulkQuoteProvider)
    assert isinstance(p, StockPerformanceProvider)
    assert isinstance(p, AllTimeHighProvider)
    assert isinstance(p, CandleProvider)
    assert isinstance(p, SectorPerformanceProvider)
    assert isinstance(p, MarketOverviewProvider)


# --- get_quotes (the batched board feed behind the heat map) ---------------------------------


class RecordingSnapshotClient:
    """A data client that records each snapshot request's symbol list and serves a fixed
    symbol->snapshot map, so a test can assert both the returned quotes and the chunking."""

    def __init__(self, snapshots_by_symbol, error=None):
        self._snapshots = snapshots_by_symbol
        self._error = error
        self.requested_chunks = []  # each call's symbol list, in order

    def get_stock_snapshot(self, request):
        symbols = request.symbol_or_symbols
        self.requested_chunks.append(list(symbols))
        if self._error is not None:
            raise self._error
        return {s: self._snapshots.get(s) for s in symbols}


def test_get_quotes_returns_a_quote_per_recognized_symbol():
    client = RecordingSnapshotClient(
        {"AAPL": make_snapshot(), "MSFT": make_snapshot()}
    )
    p = provider_with(client, ExplodingTradingClient())  # no asset-metadata call
    quotes = p.get_quotes(["AAPL", "MSFT"])
    assert set(quotes) == {"AAPL", "MSFT"}
    assert all(isinstance(q, Quote) for q in quotes.values())
    assert quotes["AAPL"].change_percent == 0.6  # (297.86 - 296.07) / 296.07 * 100


def test_get_quotes_skips_symbols_the_feed_has_no_quote_for():
    # A missing snapshot and one without a latest_trade are both dropped, not errors —
    # the board renders those tiles uncoloured.
    no_trade = make_snapshot()
    no_trade.latest_trade = None
    client = RecordingSnapshotClient(
        {"AAPL": make_snapshot(), "MSFT": None, "TSLA": no_trade}
    )
    p = provider_with(client, ExplodingTradingClient())
    quotes = p.get_quotes(["AAPL", "MSFT", "TSLA"])
    assert set(quotes) == {"AAPL"}


def test_get_quotes_dedupes_and_uppercases_input():
    client = RecordingSnapshotClient({"AAPL": make_snapshot()})
    p = provider_with(client, ExplodingTradingClient())
    p.get_quotes(["aapl", "AAPL", "aapl"])
    assert client.requested_chunks == [["AAPL"]]  # one symbol, one call


def test_get_quotes_empty_input_makes_no_call():
    client = RecordingSnapshotClient({})
    p = provider_with(client, ExplodingTradingClient())
    assert p.get_quotes([]) == {}
    assert client.requested_chunks == []


def test_get_quotes_chunks_large_symbol_lists():
    symbols = [f"S{i}" for i in range(450)]
    client = RecordingSnapshotClient({s: make_snapshot() for s in symbols})
    p = provider_with(client, ExplodingTradingClient())
    quotes = p.get_quotes(symbols)
    assert len(quotes) == 450
    # 450 symbols / 100-per-chunk -> 5 requests (100 x4, 50).
    assert [len(c) for c in client.requested_chunks] == [100, 100, 100, 100, 50]


class OneChunkFailsClient:
    """Fails the Nth snapshot call (0-indexed) with an APIError, serves the rest — so a test
    can prove a single rejected chunk doesn't discard the other chunks' quotes."""

    def __init__(self, snapshots_by_symbol, fail_index):
        self._snapshots = snapshots_by_symbol
        self._fail_index = fail_index
        self.calls = 0

    def get_stock_snapshot(self, request):
        i = self.calls
        self.calls += 1
        if i == self._fail_index:
            raise APIError("boom")
        return {s: self._snapshots.get(s) for s in request.symbol_or_symbols}


def test_get_quotes_skips_a_failed_chunk_and_keeps_the_rest():
    # 150 symbols -> two chunks of (100, 50); fail the first, and the second's 50 still return.
    symbols = [f"S{i}" for i in range(150)]
    client = OneChunkFailsClient({s: make_snapshot() for s in symbols}, fail_index=0)
    p = provider_with(client, ExplodingTradingClient())
    quotes = p.get_quotes(symbols)
    assert client.calls == 2  # kept going after the failure
    assert len(quotes) == 50  # only the surviving chunk's symbols
    assert set(quotes) == {f"S{i}" for i in range(100, 150)}


def test_get_quotes_raises_only_when_every_chunk_fails():
    # A single chunk that fails yields nothing -> a hard feed failure.
    client = RecordingSnapshotClient({}, error=APIError("boom"))
    p = provider_with(client, ExplodingTradingClient())
    with pytest.raises(StockDataUnavailable):
        p.get_quotes(["AAPL"])


def test_to_alpaca_symbol_maps_share_class_dash_to_dot():
    # Our universe stores Yahoo's dash form; Alpaca lists the dot form. Plain tickers pass
    # through untouched.
    assert AlpacaStockDataProvider._to_alpaca_symbol("BRK-B") == "BRK.B"
    assert AlpacaStockDataProvider._to_alpaca_symbol("BF-B") == "BF.B"
    assert AlpacaStockDataProvider._to_alpaca_symbol("AAPL") == "AAPL"


def test_get_quotes_requests_class_shares_in_alpaca_symbology():
    # A dashed ticker must go out to Alpaca as the dot form (else it's rejected and takes
    # its whole chunk down), but come back keyed by our original dash form.
    client = RecordingSnapshotClient({"BRK.B": make_snapshot()})
    p = provider_with(client, ExplodingTradingClient())
    quotes = p.get_quotes(["BRK-B"])
    assert client.requested_chunks == [["BRK.B"]]  # requested in Alpaca's symbology
    assert set(quotes) == {"BRK-B"}  # returned in ours
    assert quotes["BRK-B"].symbol == "BRK-B"
