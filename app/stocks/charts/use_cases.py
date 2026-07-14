"""Application Business Rules: the chart use cases.

Candles, EMA overlays and support levels — all derived from the same OHLC
bars through the one ``CandleProvider`` port. The indicator math is pure
domain logic (``indicators.py``); these use cases only fetch the window and
delegate.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from app.stocks.charts.indicators import (
    EmaSeries,
    SupportLevelSeries,
    TrendAssessment,
    _DEFAULT_FLAT_THRESHOLD_PERCENT,
    assess_trend,
    ema_series,
    support_levels,
)
from app.stocks.charts.ports import CandleProvider
from app.stocks.entities import CandleSeries, Timeframe


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker and reject obvious junk, once, at the edge of the
    use case — so every layer below sees a clean symbol. Mirrors the other slices'
    guard."""
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not normalized.isalpha() or len(normalized) > 5:
        # Simple guard; real tickers are 1-5 letters (ignoring class suffixes).
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


class GetStockCandles:
    """Use case: retrieve historical OHLC candles for charting."""

    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider

    def execute(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> CandleSeries:
        if start is not None and end is not None and start >= end:
            raise ValueError("'start' must be earlier than 'end'.")
        return self._provider.get_candles(
            _normalize_symbol(symbol), timeframe, start=start, end=end
        )


# Approximate wall-clock span of one bar at each granularity. Used only to reach
# far enough *before* the visible window to warm an EMA up (see GetStockEma). A
# daily bar spans more than a calendar day once weekends/holidays are counted, so
# the warmup applies a generous multiple rather than these raw spans.
_BAR_SPAN: dict[Timeframe, timedelta] = {
    Timeframe.MIN_1: timedelta(minutes=1),
    Timeframe.MIN_5: timedelta(minutes=5),
    Timeframe.MIN_15: timedelta(minutes=15),
    Timeframe.MIN_30: timedelta(minutes=30),
    Timeframe.HOUR_1: timedelta(hours=1),
    Timeframe.HOUR_4: timedelta(hours=4),
    Timeframe.DAY_1: timedelta(days=1),
    Timeframe.WEEK_1: timedelta(weeks=1),
    Timeframe.MONTH_1: timedelta(days=31),
}

# Reach back this many bar-spans per period of warmup. 3× comfortably covers the
# weekend/holiday gaps that stretch a daily bar past one calendar day, so a
# `period`-bar EMA is fully warm by the visible window's start.
_EMA_WARMUP_FACTOR = 3


def _ema_warmup_span(timeframe: Timeframe, max_period: int) -> timedelta:
    """How far before the visible window to start fetching so an EMA of
    ``max_period`` is already warm by that window's first bar."""
    return _BAR_SPAN.get(timeframe, timedelta(days=1)) * max_period * _EMA_WARMUP_FACTOR


class GetStockEma:
    """Use case: compute EMA overlay line(s) for a symbol from its price history.

    Reuses the CandleProvider port — EMA is derived from the same OHLC bars the
    chart endpoint uses, so no extra data source is needed. The indicator math is
    pure domain logic (``ema_series``); this use case only fetches the window and
    delegates. One or more periods can be requested in a single call (e.g. the
    9/21/50 overlay), each returned as its own line.

    **Warmup.** An EMA's first value only lands ``period - 1`` bars in, so fetching
    exactly the visible ``[start, end]`` would leave the chart's left edge bare
    (and a deep period blank). So the fetch reaches an extra ``max(period)`` bars
    *before* ``start``, computes over the longer series, then trims the result back
    to the visible window — every on-screen candle then carries a value. A ``start``
    of ``None`` (MAX) already pulls all available history, so there's nothing
    earlier to warm from and nothing to trim.
    """

    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider

    def execute(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        periods: Sequence[int],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> EmaSeries:
        if start is not None and end is not None and start >= end:
            raise ValueError("'start' must be earlier than 'end'.")
        # Extend the fetch back by a warmup so the EMA is already warm at `start`.
        fetch_start = start
        if start is not None and periods:
            fetch_start = start - _ema_warmup_span(timeframe, max(periods))
        series = self._provider.get_candles(
            _normalize_symbol(symbol), timeframe, start=fetch_start, end=end
        )
        ema = ema_series(series, periods)
        if start is None:
            return ema
        # Trim the warmup bars back off, leaving only the visible window.
        return replace(
            ema,
            lines=tuple(
                replace(
                    line,
                    points=tuple(p for p in line.points if p.timestamp >= start),
                )
                for line in ema.lines
            ),
        )


class GetStockSupportLevels:
    """Use case: detect horizontal support levels for a symbol from its price
    history.

    Reuses the CandleProvider port — support is read from the same OHLC bars the
    chart endpoint uses, so no extra data source is needed. The detection math is
    pure domain logic (``support_levels``); this use case only fetches the window
    and delegates. Too little history (or no swing low below the current price)
    yields an empty series rather than an error: the symbol exists, there just
    isn't a level to draw.
    """

    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider

    def execute(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        window: int = 5,
        tolerance: float = 0.02,
        max_levels: int = 5,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> SupportLevelSeries:
        if start is not None and end is not None and start >= end:
            raise ValueError("'start' must be earlier than 'end'.")
        series = self._provider.get_candles(
            _normalize_symbol(symbol), timeframe, start=start, end=end
        )
        return support_levels(
            series, window=window, tolerance=tolerance, max_levels=max_levels
        )


# Trend read defaults: a short and a long horizon, in candles. 20/50 is the common
# short-vs-intermediate pair on daily bars; pass 50/200 for the classic long-term
# read. The long period sets how much warmup history to reach back for.
_DEFAULT_SHORT_PERIOD = 20
_DEFAULT_LONG_PERIOD = 50


class GetStockTrend:
    """Use case: classify a symbol's short- and long-term trend from its price
    history.

    Reuses the CandleProvider port — trend is read from the same OHLC bars the chart
    endpoint uses, so no extra data source is needed. The classification is pure
    domain logic (``assess_trend``: the slope of a short and a long EMA); this use
    case validates the horizons, fetches enough history to warm the long EMA, and
    delegates.

    **Warmup.** The long EMA's slope is measured over ``long_period`` bars, so it
    needs roughly ``2 × long_period`` closes to exist. Like ``GetStockEma`` the fetch
    reaches an extra ``long_period`` bars *before* ``start`` so the read is
    well-formed even when the requested window is short — the trend as of ``end``
    shouldn't depend on the chart's zoom (the same reasoning support levels use).
    A ``start`` of ``None`` (MAX) already pulls all history, so there's nothing to
    reach back for. Unlike EMA there's no trim: trend is a single point-in-time read,
    not a line drawn across the visible window.
    """

    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider

    def execute(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        short_period: int = _DEFAULT_SHORT_PERIOD,
        long_period: int = _DEFAULT_LONG_PERIOD,
        deadband_percent: float = _DEFAULT_FLAT_THRESHOLD_PERCENT,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> TrendAssessment:
        if start is not None and end is not None and start >= end:
            raise ValueError("'start' must be earlier than 'end'.")
        if short_period < 2 or long_period < 2:
            raise ValueError("trend periods must be at least 2.")
        if short_period >= long_period:
            raise ValueError("short_period must be less than long_period.")
        # Reach back a warmup so the long EMA is warm by `start`.
        fetch_start = start
        if start is not None:
            fetch_start = start - _ema_warmup_span(timeframe, long_period)
        series = self._provider.get_candles(
            _normalize_symbol(symbol), timeframe, start=fetch_start, end=end
        )
        return assess_trend(
            series,
            short_period=short_period,
            long_period=long_period,
            deadband_percent=deadband_percent,
        )
