from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from app.stocks.adapters.yfinance.price_adapter import YahooPriceProvider
from app.stocks.entities import Timeframe
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


class _FakeTicker:
    def __init__(self, *, fast=None, frame=None, info=None, fast_error=None, history_error=None):
        self._fast = fast
        self._frame = frame
        self._info = info or {}
        self._fast_error = fast_error
        self._history_error = history_error
        self.history_calls: list[dict] = []

    @property
    def fast_info(self):
        if self._fast_error is not None:
            raise self._fast_error
        return self._fast

    def history(self, **kwargs):
        self.history_calls.append(kwargs)
        if self._history_error is not None:
            raise self._history_error
        return self._frame

    @property
    def info(self):
        return self._info


def _provider(ticker: _FakeTicker) -> YahooPriceProvider:
    return YahooPriceProvider(ticker_factory=lambda symbol: ticker)


def _fast(**kw) -> SimpleNamespace:
    base = dict(
        last_price=None, previous_close=None, open=None, day_high=None,
        day_low=None, last_volume=None, market_cap=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_quote_maps_fast_info_and_nulls_the_delayed_fields():
    ticker = _FakeTicker(fast=_fast(last_price=52.4, previous_close=50.0))
    quote = _provider(ticker).get_quote("SHOP.TO")

    assert quote.symbol == "SHOP.TO"
    assert quote.price == 52.4
    assert quote.previous_close == 50.0
    # Delayed feed: no bid/ask, and no fabricated "now" timestamp.
    assert (quote.bid, quote.ask, quote.as_of) == (None, None, None)
    # The day-move rule is the shared entity's — a slim quote agrees with the full view.
    assert quote.change == pytest.approx(2.4)
    assert quote.change_percent == pytest.approx(4.8)


def test_quote_without_a_price_is_stock_not_found():
    with pytest.raises(StockNotFound):
        _provider(_FakeTicker(fast=_fast(last_price=None))).get_quote("SHOP.TO")


def test_quote_with_a_nonpositive_price_is_stock_not_found():
    with pytest.raises(StockNotFound):
        _provider(_FakeTicker(fast=_fast(last_price=0.0))).get_quote("SHOP.TO")


def test_quote_fast_info_failure_is_data_unavailable():
    ticker = _FakeTicker(fast_error=RuntimeError("yahoo blocked"))
    with pytest.raises(StockDataUnavailable):
        _provider(ticker).get_quote("SHOP.TO")


def test_stock_maps_fast_info_and_best_effort_name_exchange():
    ticker = _FakeTicker(
        fast=_fast(
            last_price=52.4, previous_close=50.0, open=51.0, day_high=53.0,
            day_low=50.5, last_volume=1234, market_cap=6.5e10,
        ),
        info={"longName": "Shopify Inc.", "fullExchangeName": "Toronto"},
    )
    stock = _provider(ticker).get_stock("SHOP.TO")

    assert (stock.symbol, stock.name, stock.exchange) == ("SHOP.TO", "Shopify Inc.", "Toronto")
    assert (stock.price, stock.open, stock.high, stock.low) == (52.4, 51.0, 53.0, 50.5)
    assert stock.previous_close == 50.0
    assert stock.volume == 1234
    assert stock.market_cap == 6.5e10
    assert (stock.bid, stock.ask, stock.as_of) == (None, None, None)


def test_stock_name_exchange_are_best_effort_when_info_fails():
    class _InfoBoom(_FakeTicker):
        @property
        def info(self):
            raise RuntimeError("info blocked")

    ticker = _InfoBoom(fast=_fast(last_price=52.4))
    stock = _provider(ticker).get_stock("SHOP.TO")
    assert (stock.name, stock.exchange) == (None, None)  # never fatal
    assert stock.price == 52.4


def _frame(rows) -> pd.DataFrame:
    index = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Volume": [r[5] for r in rows],
        },
        index=index,
    )


def test_candles_map_chronologically_and_drop_nan_rows():
    frame = _frame(
        [
            ("2026-06-01", 10.0, 11.0, 9.5, 10.5, 1000),
            ("2026-06-02", 10.5, 10.5, 9.0, float("nan"), 900),  # no close -> dropped
            ("2026-06-03", 10.5, 12.0, 10.0, 11.8, 1500),
        ]
    )
    series = _provider(_FakeTicker(frame=frame)).get_candles(
        "SHOP.TO", Timeframe.DAY_1, start=None, end=None
    )
    assert series.symbol == "SHOP.TO"
    assert [c.close for c in series.candles] == [10.5, 11.8]  # NaN row dropped, chronological
    assert series.candles[0].volume == 1000
    assert series.candles[0].is_bullish is True  # 10.5 close >= 10.0 open
    # Naive daily index is read as UTC midnight.
    assert series.candles[0].timestamp == datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_candles_four_hour_is_unsupported():
    # yfinance has no 4h granularity — reject rather than silently return a different bar size.
    with pytest.raises(StockDataUnavailable):
        _provider(_FakeTicker(frame=_frame([]))).get_candles(
            "SHOP.TO", Timeframe.HOUR_4, start=None, end=None
        )


def test_candles_empty_history_is_stock_not_found():
    with pytest.raises(StockNotFound):
        _provider(_FakeTicker(frame=_frame([]))).get_candles(
            "SHOP.TO", Timeframe.DAY_1, start=None, end=None
        )


def test_candles_history_failure_is_data_unavailable():
    ticker = _FakeTicker(history_error=RuntimeError("yahoo blocked"))
    with pytest.raises(StockDataUnavailable):
        _provider(ticker).get_candles("SHOP.TO", Timeframe.DAY_1, start=None, end=None)


def test_candles_map_the_timeframe_to_a_yfinance_interval():
    ticker = _FakeTicker(frame=_frame([("2026-06-03", 1, 1, 1, 1, 1)]))
    _provider(ticker).get_candles("SHOP.TO", Timeframe.WEEK_1, start=None, end=None)
    assert ticker.history_calls[-1]["interval"] == "1wk"


def test_performance_computes_windows_from_daily_closes():
    # 15 consecutive daily bars, all close 100 except the last (110). The one-week window's
    # base is the bar 7 days back (still 100) -> +10%; the longer windows have no base in this
    # short history, and YTD has no prior-year bar, so both are None.
    rows = []
    dates = pd.date_range(end="2026-06-15", periods=15, freq="D")
    for i, d in enumerate(dates):
        close = 110.0 if i == len(dates) - 1 else 100.0
        rows.append((d.isoformat(), close, close, close, close, 1000))
    perf = _provider(_FakeTicker(frame=_frame(rows))).get_performance("SHOP.TO")

    assert perf.one_week == pytest.approx(10.0)
    assert perf.one_month is None  # only 15 days of history
    assert perf.ytd is None  # no prior-year close in the window


def test_performance_on_empty_history_is_all_none():
    perf = _provider(_FakeTicker(frame=_frame([]))).get_performance("SHOP.TO")
    assert perf.one_week is None and perf.one_year is None and perf.ytd is None


def test_performance_history_failure_is_data_unavailable():
    ticker = _FakeTicker(history_error=RuntimeError("yahoo blocked"))
    with pytest.raises(StockDataUnavailable):
        _provider(ticker).get_performance("SHOP.TO")


def test_all_time_high_from_full_history():
    frame = _frame(
        [
            ("2024-01-02", 48, 50.0, 47, 49, 100),  # earliest date covered
            ("2025-06-01", 118, 120.0, 115, 119, 100),  # the peak high
            ("2026-03-01", 88, 90.0, 86, 89, 100),
        ]
    )
    ath = _provider(_FakeTicker(frame=frame)).get_all_time_high("SHOP.TO")
    assert ath.price == 120.0
    assert ath.reached_on == datetime(2025, 6, 1).date()
    assert ath.since == datetime(2024, 1, 2).date()  # the "all-time" bound


def test_all_time_high_requests_the_max_period():
    ticker = _FakeTicker(frame=_frame([("2025-01-02", 1, 2, 1, 1.5, 1)]))
    _provider(ticker).get_all_time_high("SHOP.TO")
    assert ticker.history_calls[-1]["period"] == "max"


def test_all_time_high_empty_history_is_stock_not_found():
    with pytest.raises(StockNotFound):
        _provider(_FakeTicker(frame=_frame([]))).get_all_time_high("SHOP.TO")


def test_all_time_high_history_failure_is_data_unavailable():
    ticker = _FakeTicker(history_error=RuntimeError("yahoo blocked"))
    with pytest.raises(StockDataUnavailable):
        _provider(ticker).get_all_time_high("SHOP.TO")
