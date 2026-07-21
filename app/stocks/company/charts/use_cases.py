from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from app.stocks.company.charts.indicators import (
    EmaSeries,
    IndicatorSet,
    IndicatorSpec,
    SupportLevelSeries,
    TrendAssessment,
    _DEFAULT_FLAT_THRESHOLD_PERCENT,
    _DEFAULT_PRICE_FLAT_THRESHOLD_PERCENT,
    assess_trend,
    build_indicators,
    ema_series,
    indicator_warmup_bars,
    support_levels,
)
from app.stocks.company.charts.ports import CandleProvider
from app.stocks.entities import CandleSeries, Timeframe, normalize_symbol


def _normalize_symbol(symbol: str) -> str:
    return normalize_symbol(symbol)


class GetStockCandles:
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
# `period`-bar indicator is fully warm by the visible window's start.
_WARMUP_FACTOR = 3


def _warmup_span(timeframe: Timeframe, max_period: int) -> timedelta:
    return _BAR_SPAN.get(timeframe, timedelta(days=1)) * max_period * _WARMUP_FACTOR


class GetStockEma:
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
            fetch_start = start - _warmup_span(timeframe, max(periods))
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


# Trend read defaults: a short, a medium and a long horizon, in candles. 20/50/200 is
# the classic moving-average trio on daily bars — near-term, intermediate, and primary
# trend. The long period sets how much warmup history to reach back for.
_DEFAULT_SHORT_PERIOD = 20
_DEFAULT_MEDIUM_PERIOD = 50
_DEFAULT_LONG_PERIOD = 200


class GetStockTrend:
    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider

    def execute(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        short_period: int = _DEFAULT_SHORT_PERIOD,
        medium_period: int = _DEFAULT_MEDIUM_PERIOD,
        long_period: int = _DEFAULT_LONG_PERIOD,
        deadband_percent: float = _DEFAULT_FLAT_THRESHOLD_PERCENT,
        price_deadband_percent: float = _DEFAULT_PRICE_FLAT_THRESHOLD_PERCENT,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> TrendAssessment:
        if start is not None and end is not None and start >= end:
            raise ValueError("'start' must be earlier than 'end'.")
        if short_period < 2 or medium_period < 2 or long_period < 2:
            raise ValueError("trend periods must be at least 2.")
        if not short_period < medium_period < long_period:
            raise ValueError(
                "short_period < medium_period < long_period is required."
            )
        # Reach back a warmup so the long EMA (the deepest horizon) is warm by `start`.
        fetch_start = start
        if start is not None:
            fetch_start = start - _warmup_span(timeframe, long_period)
        series = self._provider.get_candles(
            _normalize_symbol(symbol), timeframe, start=fetch_start, end=end
        )
        return assess_trend(
            series,
            short_period=short_period,
            medium_period=medium_period,
            long_period=long_period,
            deadband_percent=deadband_percent,
            price_deadband_percent=price_deadband_percent,
        )


class GetStockIndicators:
    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider

    def execute(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        specs: Sequence[IndicatorSpec],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> IndicatorSet:
        if not specs:
            raise ValueError("At least one indicator is required.")
        if start is not None and end is not None and start >= end:
            raise ValueError("'start' must be earlier than 'end'.")
        # Reach back a warmup sized to the deepest requested indicator.
        fetch_start = start
        if start is not None:
            max_warmup = max(indicator_warmup_bars(s.name, s.period) for s in specs)
            if max_warmup > 0:
                fetch_start = start - _warmup_span(timeframe, max_warmup)
        series = self._provider.get_candles(
            _normalize_symbol(symbol), timeframe, start=fetch_start, end=end
        )
        result = build_indicators(series, specs)
        if start is None:
            return result
        # Trim the warmup bars back off each line, leaving only the visible window.
        return replace(
            result,
            indicators=tuple(
                replace(
                    indicator,
                    lines=tuple(
                        replace(
                            line,
                            points=tuple(
                                p for p in line.points if p.timestamp >= start
                            ),
                        )
                        for line in indicator.lines
                    ),
                )
                for indicator in result.indicators
            ),
        )
