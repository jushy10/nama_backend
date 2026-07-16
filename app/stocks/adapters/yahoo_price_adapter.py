"""Interface Adapter: live price data from Yahoo Finance (via ``yfinance``), keyless.

The price-feed counterpart to the Alpaca adapter, for the markets Alpaca doesn't cover —
the Canadian listings (TSX/TSXV, suffixed ``.TO`` / ``.V``). It implements the same shared
price ports off Yahoo:

- ``StockQuoteProvider.get_quote`` — ``Ticker.fast_info`` (last price + previous close).
- ``CandleProvider.get_candles`` — ``Ticker.history(interval=…)`` → OHLC bars.
- ``StockPerformanceProvider.get_performance`` — a year of daily ``history`` → trailing windows.
- ``AllTimeHighProvider.get_all_time_high`` — the full daily ``history`` → the highest high +
  when + the earliest date covered (the analysis context's drawdown-from-high input).
- ``StockDataProvider.get_stock`` — ``fast_info`` price fields + a best-effort ``.info`` for
  the name/exchange.

It's the only module besides the other yfinance adapters that knows Yahoo backs a price view;
swap it and only this file changes. Two deliberate properties of the source, surfaced honestly
rather than hidden:

- **Delayed & thin.** Yahoo's quote is an ~15-minute-delayed last price with **no bid/ask** and
  **no reliable trade timestamp**, so ``Quote.bid`` / ``ask`` / ``as_of`` come back ``None`` (a
  fabricated ``now()`` would misrepresent a delayed print). The card treats this feed's quote as
  the primary read for a CA symbol, but the FE should label it delayed.
- **Best-effort transport.** Yahoo intermittently rate-limits data-centre IPs; every access goes
  through the shared ``yfinance_session`` crumb-401 retry, and any hard failure becomes
  ``StockDataUnavailable`` (a symbol with no data → ``StockNotFound``), so a routing caller can
  fall back or leave the block null.

Yahoo has **no 4-hour granularity**, so a ``Timeframe.HOUR_4`` candle request raises
``StockDataUnavailable`` rather than silently returning a different bar size. The window math
mirrors the Alpaca adapter's ``_compute_performance`` (a pure calc over ``(date, close)`` pairs);
it's duplicated here rather than shared to avoid one adapter importing another.
"""

from __future__ import annotations

import bisect
import math
from datetime import datetime, timedelta, timezone

import yfinance as yf

from app.stocks.adapters import yfinance_session
from app.stocks.entities import (
    AllTimeHigh,
    Candle,
    CandleSeries,
    Quote,
    Stock,
    StockPerformance,
    Timeframe,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import (
    AllTimeHighProvider,
    StockDataProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.charts.ports import CandleProvider

# Our business-level granularities → yfinance's interval strings. HOUR_4 has no yfinance
# equivalent (its intervals jump 1h → 1d), so it's absent and rejected explicitly rather than
# mapped to a different bar size.
_INTERVAL_MAP: dict[Timeframe, str] = {
    Timeframe.MIN_1: "1m",
    Timeframe.MIN_5: "5m",
    Timeframe.MIN_15: "15m",
    Timeframe.MIN_30: "30m",
    Timeframe.HOUR_1: "60m",
    Timeframe.DAY_1: "1d",
    Timeframe.WEEK_1: "1wk",
    Timeframe.MONTH_1: "1mo",
}

# Trailing-performance lookback: a bit over a year of daily bars so the 1Y window has a base.
_PERFORMANCE_LOOKBACK_DAYS = 400

# Each trailing window's length in days (YTD is handled separately — it's calendar-anchored).
_WINDOW_DAYS = {
    "one_week": 7,
    "one_month": 30,
    "three_month": 91,
    "six_month": 182,
    "one_year": 365,
}


class YahooPriceProvider(
    StockDataProvider,
    StockQuoteProvider,
    StockPerformanceProvider,
    AllTimeHighProvider,
    CandleProvider,
):
    """Live price data from Yahoo (``yfinance``) for the markets Alpaca doesn't serve."""

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker (canned fast_info / history) instead of
        # reaching Yahoo; defaults to the real thing.
        self._ticker_factory = ticker_factory or yf.Ticker

    # --- StockQuoteProvider ---

    def get_quote(self, symbol: str) -> Quote:
        fast = self._fast_info(symbol)
        price = _float(_fast_get(fast, "last_price", "lastPrice"))
        if price is None or price <= 0:
            raise StockNotFound(symbol)
        return Quote(
            symbol=symbol,
            price=price,
            previous_close=_float(_fast_get(fast, "previous_close", "previousClose")),
            bid=None,  # Yahoo's delayed feed carries no bid/ask
            ask=None,
            as_of=None,  # no reliable trade timestamp — a delayed print, not "now"
        )

    # --- StockDataProvider ---

    def get_stock(self, symbol: str) -> Stock:
        fast = self._fast_info(symbol)
        price = _float(_fast_get(fast, "last_price", "lastPrice"))
        if price is None or price <= 0:
            raise StockNotFound(symbol)
        name, exchange = self._name_and_exchange(symbol)
        return Stock(
            symbol=symbol,
            name=name,
            exchange=exchange,
            price=price,
            open=_float(_fast_get(fast, "open")),
            high=_float(_fast_get(fast, "day_high", "dayHigh")),
            low=_float(_fast_get(fast, "day_low", "dayLow")),
            previous_close=_float(_fast_get(fast, "previous_close", "previousClose")),
            volume=_int(_fast_get(fast, "last_volume", "lastVolume")),
            bid=None,
            ask=None,
            as_of=None,
            market_cap=_float(_fast_get(fast, "market_cap", "marketCap")),
        )

    # --- StockPerformanceProvider ---

    def get_performance(self, symbol: str) -> StockPerformance:
        start = datetime.now(timezone.utc) - timedelta(days=_PERFORMANCE_LOOKBACK_DAYS)
        frame = self._history(symbol, interval="1d", start=start)
        return _compute_performance(_close_series(frame))

    # --- AllTimeHighProvider ---

    def get_all_time_high(self, symbol: str) -> AllTimeHigh:
        # The full daily history Yahoo carries (split/dividend-adjusted, so old highs stay
        # comparable to today's adjusted price — the same basis as the candle/performance reads).
        frame = self._history(symbol, interval="1d", period="max")
        high = _to_all_time_high(frame)
        if high is None:
            raise StockNotFound(symbol)  # no history — the best-effort wrapper omits the field
        return high

    # --- CandleProvider ---

    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> CandleSeries:
        interval = _INTERVAL_MAP.get(timeframe)
        if interval is None:
            # HOUR_4 has no yfinance equivalent — don't silently return a different bar size.
            raise StockDataUnavailable(
                symbol, f"{timeframe.value} candles aren't available from this source"
            )
        frame = self._history(symbol, interval=interval, start=start, end=end)
        candles = tuple(_to_candles(frame))
        if not candles:
            raise StockNotFound(symbol)
        return CandleSeries(symbol=symbol, timeframe=timeframe, candles=candles)

    # --- Yahoo calls (thin and isolated, through the shared crumb-retry seam) ---

    def _fast_info(self, symbol: str):
        """The symbol's ``fast_info``, with the price fetch forced inside the crumb-retry seam
        (a raised or swallowed 401 drops the cached crumb and retries once)."""
        ticker = self._ticker_factory(symbol)

        def read():
            fast = ticker.fast_info
            _fast_get(fast, "last_price", "lastPrice")  # force the lazy network fetch
            return fast

        try:
            return yfinance_session.call(
                read,
                is_empty=lambda f: _fast_get(f, "last_price", "lastPrice") is None,
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(symbol, f"yfinance fast_info failed ({exc})") from exc

    def _history(self, symbol, *, interval, start=None, end=None, period=None):
        """A history DataFrame over the window (or ``period`` — e.g. ``"max"`` for the all-time
        high), through the crumb-retry seam (an empty frame is the swallowed-401 signature).
        Split/dividend-adjusted for a continuous line."""
        ticker = self._ticker_factory(symbol)

        def read():
            if period is not None:
                return ticker.history(
                    period=period,
                    interval=interval,
                    auto_adjust=True,
                    actions=False,
                )
            return ticker.history(
                start=start,
                end=end,
                interval=interval,
                auto_adjust=True,
                actions=False,
            )

        try:
            return yfinance_session.call(read, is_empty=yfinance_session.frame_is_empty)
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(symbol, f"yfinance history failed ({exc})") from exc

    def _name_and_exchange(self, symbol: str) -> tuple[str | None, str | None]:
        """Company name + listing exchange from ``.info`` — best-effort, never fatal (the
        gated, heavier read); a failure just leaves both ``None`` so a CA card falls back to
        the exchange the screen already stamped."""
        try:
            info = yfinance_session.call(lambda: self._ticker_factory(symbol).info)
        except Exception:  # noqa: BLE001 — best-effort enrichment
            return None, None
        if not isinstance(info, dict):
            return None, None
        name = info.get("longName") or info.get("shortName")
        exchange = info.get("fullExchangeName") or info.get("exchange")
        return (name or None), (exchange or None)


# --- Pure mapping helpers (pandas/NaN handling stays here) ---


def _to_candles(frame):
    """A history DataFrame → chronological ``Candle``s (oldest first, the order yfinance
    already returns). Rows without a usable close are dropped."""
    if frame is None or getattr(frame, "empty", True):
        return
    for ts, row in frame.iterrows():
        close = _float(row.get("Close"))
        open_ = _float(row.get("Open"))
        high = _float(row.get("High"))
        low = _float(row.get("Low"))
        if close is None or open_ is None or high is None or low is None:
            continue
        yield Candle(
            timestamp=_to_utc(ts),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=_int(row.get("Volume")),
        )


def _close_series(frame) -> list[tuple[object, float]]:
    """A history DataFrame → ``(date, close)`` pairs (ascending), the input to the window calc.
    Rows without a usable close are dropped."""
    if frame is None or getattr(frame, "empty", True):
        return []
    series: list[tuple[object, float]] = []
    for ts, row in frame.iterrows():
        close = _float(row.get("Close"))
        if close is not None:
            series.append((_to_utc(ts).date(), close))
    return series


def _to_all_time_high(frame) -> AllTimeHigh | None:
    """A daily-history DataFrame → the highest intraday high, the day it was reached, and the
    earliest date the history covers (the "all-time" bound). ``None`` when the frame is empty
    or carries no usable high — the same shape the Alpaca adapter derives from its bars."""
    if frame is None or getattr(frame, "empty", True):
        return None
    peak_price: float | None = None
    peak_date = None
    earliest = None
    for ts, row in frame.iterrows():
        high = _float(row.get("High"))
        if high is None:
            continue
        day = _to_utc(ts).date()
        if earliest is None or day < earliest:
            earliest = day
        if peak_price is None or high > peak_price:
            peak_price = high
            peak_date = day
    if peak_price is None:
        return None
    return AllTimeHigh(price=peak_price, reached_on=peak_date, since=earliest)


def _compute_performance(points: list[tuple[object, float]]) -> StockPerformance:
    """Percent change of the latest close vs the close starting each window — the same
    algorithm the Alpaca adapter uses, over ``(date, close)`` pairs. All-``None`` when empty."""
    if not points:
        return StockPerformance(None, None, None, None, None, None)
    points = sorted(points, key=lambda p: p[0])  # ascending by date; defensive
    dates = [p[0] for p in points]
    closes = [p[1] for p in points]
    current = closes[-1]
    anchor = dates[-1]

    def pct_since(target_date) -> float | None:
        idx = bisect.bisect_right(dates, target_date) - 1  # last bar <= target
        if idx < 0:
            return None
        base = closes[idx]
        return round((current - base) / base * 100, 2) if base else None

    return StockPerformance(
        one_week=pct_since(anchor - timedelta(days=_WINDOW_DAYS["one_week"])),
        one_month=pct_since(anchor - timedelta(days=_WINDOW_DAYS["one_month"])),
        three_month=pct_since(anchor - timedelta(days=_WINDOW_DAYS["three_month"])),
        six_month=pct_since(anchor - timedelta(days=_WINDOW_DAYS["six_month"])),
        ytd=_ytd(dates, closes, current, anchor.year),
        one_year=pct_since(anchor - timedelta(days=_WINDOW_DAYS["one_year"])),
    )


def _ytd(dates, closes, current, anchor_year) -> float | None:
    """Percent change vs the previous year's final close."""
    for i in range(len(dates) - 1, -1, -1):
        if dates[i].year < anchor_year:  # most recent bar before this year
            base = closes[i]
            return round((current - base) / base * 100, 2) if base else None
    return None


def _to_utc(ts) -> datetime:
    """A pandas Timestamp (tz-aware or naive) → a UTC ``datetime``. A naive daily index is
    treated as a UTC midnight; a tz-aware intraday index is converted."""
    dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fast_get(fast, *names):
    """Read the first present value from a ``fast_info`` by attribute (its stable snake_case
    API) then mapping key — tolerant of the FastInfo object, a plain dict, or a test double."""
    for name in names:
        value = None
        try:
            value = getattr(fast, name)
        except Exception:  # noqa: BLE001 — FastInfo raises on a missing/blocked key
            value = None
        if value is None and hasattr(fast, "get"):
            try:
                value = fast.get(name)
            except Exception:  # noqa: BLE001
                value = None
        if value is not None:
            return value
    return None


def _float(value) -> float | None:
    """A finite float, or ``None`` (NaN / non-numeric / missing)."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result


def _int(value) -> int | None:
    """A non-negative int, or ``None`` (NaN / non-numeric / missing)."""
    number = _float(value)
    return None if number is None else int(number)
