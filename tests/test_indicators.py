"""Unit tests for the pure price-series indicators (no I/O, no framework).

These are enterprise business logic, so they're tested directly on close prices
and candle series. Values here are hand-verifiable: EMA runs are worked out by
hand against the smoothing multiplier, and support levels are read off small
hand-drawn swing-low shapes.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.stocks.entities import Candle, CandleSeries, Timeframe
from app.stocks.charts.indicators import (
    SupportStrength,
    TrendDirection,
    TrendReading,
    assess_trend,
    compute_ema,
    compute_support_levels,
    ema_line,
    ema_series,
    horizon_trend,
    support_levels,
)


def _candles(closes: list[float], timeframe: Timeframe = Timeframe.DAY_1):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(
        Candle(
            timestamp=base + timedelta(days=i),
            open=c,
            high=c,
            low=c,
            close=c,
            volume=1000,
        )
        for i, c in enumerate(closes)
    )
    return CandleSeries(symbol="AAPL", timeframe=timeframe, candles=candles)


# --------------------------- compute_ema (pure math) ---------------------------


def test_compute_ema_rejects_period_below_one():
    with pytest.raises(ValueError):
        compute_ema([1.0, 2.0, 3.0], period=0)


def test_compute_ema_empty_when_not_enough_history():
    # Need at least `period` closes to seed the average.
    assert compute_ema([5.0], period=2) == []
    assert compute_ema([], period=2) == []


def test_compute_ema_matches_hand_computation():
    # closes [2,4,6,8], period 2, k = 2/(2+1) = 2/3:
    #   seed = (2+4)/2 = 3.0
    #   6*2/3 + 3*1/3 = 5.0
    #   8*2/3 + 5*1/3 = 7.0
    assert compute_ema([2.0, 4.0, 6.0, 8.0], period=2) == [3.0, 5.0, 7.0]


def test_compute_ema_flat_series_holds_the_level():
    assert compute_ema([5.0, 5.0, 5.0], period=2) == [5.0, 5.0]


def test_compute_ema_period_one_is_the_price_itself():
    # k = 1 -> every value is just that bar's close.
    assert compute_ema([2.0, 4.0, 6.0], period=1) == [2.0, 4.0, 6.0]


# --------------------------- ema_series (assembly over candles) ---------------------------


def test_ema_line_aligns_values_to_their_candle():
    series = _candles([2.0, 4.0, 6.0, 8.0])
    line = ema_line(series, period=2)
    # The seed consumes the first `period` closes; its value dates the last of
    # them (candles[period - 1]).
    assert [p.value for p in line.points] == [3.0, 5.0, 7.0]
    assert [p.timestamp for p in line.points] == [
        series.candles[1].timestamp,
        series.candles[2].timestamp,
        series.candles[3].timestamp,
    ]
    assert line.latest.value == 7.0


def test_ema_line_empty_points_when_history_too_short():
    line = ema_line(_candles([10.0, 11.0]), period=50)
    assert line.points == ()
    assert line.latest is None


def test_ema_series_one_line_per_period_in_request_order():
    result = ema_series(_candles([1.0, 2.0, 3.0, 4.0], Timeframe.HOUR_1), periods=[3, 2])
    assert result.symbol == "AAPL"
    assert result.timeframe is Timeframe.HOUR_1
    assert [line.period for line in result.lines] == [3, 2]  # order preserved


# --------------------------- compute_support_levels (pure math) ---------------------------

# A double-bottom "W": swing lows at 3.0 on indices 2 and 6, series ending at 5.0.
W_LOWS = [5.0, 4.0, 3.0, 4.0, 5.0, 4.0, 3.0, 4.0, 5.0]


def _times(n: int) -> list[datetime]:
    """n consecutive daily UTC timestamps — the bars the lows fall on."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [base + timedelta(days=i) for i in range(n)]


def test_support_two_swing_lows_at_one_price_cluster_into_a_moderate_level():
    times = _times(len(W_LOWS))
    levels = compute_support_levels(W_LOWS, times, 5.0, window=2, tolerance=0.02)
    assert len(levels) == 1
    level = levels[0]
    assert level.price == 3.0
    assert level.touches == 2
    assert level.strength is SupportStrength.MODERATE
    assert level.distance_percent == -40.0  # (3 - 5) / 5 * 100
    assert level.last_touched == times[6].date()  # the more recent of the two


def test_support_three_touches_is_strong():
    lows = [5.0, 4.0, 3.0, 4.0, 5.0, 4.0, 3.0, 4.0, 5.0, 4.0, 3.0, 4.0, 5.0]
    levels = compute_support_levels(lows, _times(len(lows)), 5.0, window=2)
    assert len(levels) == 1
    assert levels[0].touches == 3
    assert levels[0].strength is SupportStrength.STRONG


def test_support_single_touch_is_weak():
    lows = [5.0, 4.0, 3.0, 4.0, 5.0]
    levels = compute_support_levels(lows, _times(len(lows)), 5.0, window=2)
    assert [level.touches for level in levels] == [1]
    assert levels[0].strength is SupportStrength.WEAK


def test_support_excludes_levels_at_or_above_the_reference_price():
    # Troughs at 10 (a former support, now above the quote) and 6 (real support).
    lows = [12.0, 11.0, 10.0, 11.0, 12.0, 7.0, 6.0, 7.0, 8.0]
    levels = compute_support_levels(lows, _times(len(lows)), 8.0, window=2)
    assert [level.price for level in levels] == [6.0]  # the 10.0 trough is dropped


def test_support_drops_a_level_a_later_candle_closed_below():
    # Double bottom at 3.0 (idx 2 & 6), but a later bar (idx 7) closed at 2.5 —
    # below the level and after its most recent touch. Support taken out -> gone.
    closes = [5.0, 4.0, 3.0, 4.0, 5.0, 4.0, 3.0, 2.5, 5.0]
    levels = compute_support_levels(
        W_LOWS, _times(len(W_LOWS)), 5.0, closes=closes, window=2
    )
    assert levels == []


def test_support_keeps_a_level_only_wicked_below_but_closed_above():
    # Same double bottom; a later bar dipped (its low pierced the level) but closed
    # back above at 3.5. A wick is not a break — the level holds.
    closes = [5.0, 4.0, 3.0, 4.0, 5.0, 4.0, 3.0, 3.5, 5.0]
    levels = compute_support_levels(
        W_LOWS, _times(len(W_LOWS)), 5.0, closes=closes, window=2
    )
    assert [level.price for level in levels] == [3.0]


def test_support_keeps_a_level_reclaimed_after_an_earlier_close_below():
    # A close below the level at idx 3 sits *before* the level's most recent touch
    # (idx 6), so the later touch reclaimed it — not a break. The level survives.
    closes = [5.0, 4.0, 3.0, 2.5, 3.0, 4.0, 3.0, 4.0, 5.0]
    levels = compute_support_levels(
        W_LOWS, _times(len(W_LOWS)), 5.0, closes=closes, window=2
    )
    assert [level.price for level in levels] == [3.0]


def test_support_rejects_closes_length_mismatch():
    with pytest.raises(ValueError):
        compute_support_levels(
            W_LOWS, _times(len(W_LOWS)), 5.0, closes=[1.0, 2.0], window=2
        )


def test_support_merges_lows_within_tolerance():
    lows = [5.0, 4.0, 3.00, 4.0, 5.0, 4.0, 3.05, 4.0, 5.0]  # 3.00 & 3.05 ~1.7% apart
    levels = compute_support_levels(lows, _times(len(lows)), 5.0, window=2, tolerance=0.02)
    assert len(levels) == 1
    assert levels[0].touches == 2


def test_support_splits_lows_beyond_tolerance():
    lows = [5.0, 4.0, 3.00, 4.0, 5.0, 4.0, 3.60, 4.0, 5.0]  # 3.00 & 3.60 20% apart
    levels = compute_support_levels(lows, _times(len(lows)), 5.0, window=2, tolerance=0.02)
    assert [level.price for level in levels] == [3.6, 3.0]  # two levels, nearest first


def test_support_caps_at_max_levels_and_keeps_the_most_recent_on_a_tie():
    # Three single-touch troughs (7, 5, 4); with max_levels=2 the two most-recent win.
    lows = [9.0, 8.0, 7.0, 8.0, 9.0, 9.0, 5.0, 9.0, 9.0, 9.0, 4.0, 9.0, 9.0]
    levels = compute_support_levels(lows, _times(len(lows)), 10.0, window=2, max_levels=2)
    assert [level.price for level in levels] == [5.0, 4.0]  # 7.0 (oldest) dropped


def test_support_empty_when_history_too_short():
    assert compute_support_levels([3.0, 2.0, 3.0], _times(3), 5.0, window=2) == []


def test_support_empty_when_series_is_monotonic():
    lows = [9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0]  # no trough — nothing turns back up
    assert compute_support_levels(lows, _times(len(lows)), 5.0, window=2) == []


def test_support_empty_when_reference_price_non_positive():
    assert compute_support_levels(W_LOWS, _times(len(W_LOWS)), 0.0, window=2) == []


@pytest.mark.parametrize(
    "kwargs",
    [{"window": 1}, {"tolerance": 0.0}, {"tolerance": 1.0}, {"max_levels": 0}],
)
def test_support_rejects_bad_parameters(kwargs):
    with pytest.raises(ValueError):
        compute_support_levels([1.0, 2.0, 3.0, 2.0, 1.0], _times(5), 3.0, **kwargs)


def test_support_rejects_length_mismatch():
    with pytest.raises(ValueError):
        compute_support_levels([1.0, 2.0, 3.0], _times(2), 3.0, window=2)


# --------------------------- support_levels (assembly over candles) ---------------------------


def _series_with_lows(lows: list[float], last_close: float | None = None) -> CandleSeries:
    """A daily series whose bars carry the given lows; the final close (defaulting
    to the last low) is the reference price the levels are measured against."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(
        Candle(
            timestamp=base + timedelta(days=i),
            open=low,
            high=low + 0.5,
            low=low,
            close=(last_close if last_close is not None and i == len(lows) - 1 else low),
            volume=1000,
        )
        for i, low in enumerate(lows)
    )
    return CandleSeries(symbol="AAPL", timeframe=Timeframe.DAY_1, candles=candles)


def test_support_levels_uses_the_latest_close_as_reference():
    series = _series_with_lows(W_LOWS, last_close=5.0)
    result = support_levels(series, window=2)
    assert result.symbol == "AAPL"
    assert result.timeframe is Timeframe.DAY_1
    assert result.reference_price == 5.0
    assert [level.price for level in result.levels] == [3.0]
    assert result.levels[0].touches == 2


def _series_with_lows_and_closes(bars: list[tuple[float, float]]) -> CandleSeries:
    """A daily series from explicit (low, close) pairs — for asserting the break
    rule, where a bar's close diverges from its low (a wick vs. a close-below)."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(
        Candle(
            timestamp=base + timedelta(days=i),
            open=low,
            high=max(low, close) + 0.5,
            low=low,
            close=close,
            volume=1000,
        )
        for i, (low, close) in enumerate(bars)
    )
    return CandleSeries(symbol="AAPL", timeframe=Timeframe.DAY_1, candles=candles)


# Double bottom at 10.0 (swing lows at idx 2 & 6); price then recovers.
_DOUBLE_BOTTOM_LOWS = [12.0, 11.0, 10.0, 11.0, 12.0, 11.0, 10.0, 11.0, 12.0, 12.0]


def test_support_levels_drops_a_level_a_candle_closed_below():
    # A later bar closes at 9.0, below the 10.0 support and after its last touch,
    # then price recovers to 11.0. The support was taken out -> not returned.
    bars = [(low, low) for low in _DOUBLE_BOTTOM_LOWS] + [(9.0, 9.0), (11.0, 11.0)]
    result = support_levels(_series_with_lows_and_closes(bars), window=2)
    assert result.levels == ()


def test_support_levels_keeps_a_level_only_wicked_through():
    # Same shape, but the intruding bar only *wicks* to 9.0 and closes back at 11.0.
    # A wick is not a break — the 10.0 support still stands.
    bars = [(low, low) for low in _DOUBLE_BOTTOM_LOWS] + [(9.0, 11.0), (11.0, 11.0)]
    result = support_levels(_series_with_lows_and_closes(bars), window=2)
    assert [level.price for level in result.levels] == [10.0]


def test_support_levels_empty_series_is_graceful():
    result = support_levels(
        CandleSeries(symbol="AAPL", timeframe=Timeframe.DAY_1, candles=())
    )
    assert result.levels == ()
    assert result.reference_price == 0.0


# --------------------------- horizon_trend (pure trend read) ---------------------------


def test_horizon_trend_none_when_too_little_history():
    # One EMA point (or none) can't form a slope.
    assert horizon_trend([10.0, 11.0], period=2) is None  # exactly one EMA point
    assert horizon_trend([10.0], period=2) is None
    assert horizon_trend([], period=2) is None


def test_horizon_trend_rising_series_is_up():
    trend = horizon_trend([float(c) for c in range(10, 25)], period=3)
    assert trend is not None
    assert trend.direction is TrendDirection.UP
    assert trend.slope_percent > 0
    assert trend.change_percent > 0
    # Price leads a rising EMA, so the latest close sits above it.
    assert trend.price_vs_ema_percent > 0


def test_horizon_trend_falling_series_is_down():
    trend = horizon_trend([float(c) for c in range(25, 10, -1)], period=3)
    assert trend is not None
    assert trend.direction is TrendDirection.DOWN
    assert trend.slope_percent < 0
    assert trend.price_vs_ema_percent < 0


def test_horizon_trend_flat_series_is_sideways():
    trend = horizon_trend([50.0] * 12, period=3)
    assert trend is not None
    assert trend.direction is TrendDirection.SIDEWAYS
    assert trend.slope_percent == 0.0
    assert trend.change_percent == 0.0


def test_horizon_trend_gentle_drift_reads_sideways_under_deadband():
    # A slope well inside the deadband is flat; a tiny deadband lets it read up.
    closes = [100.0 + i * 0.01 for i in range(12)]  # ~0.01/bar drift
    assert horizon_trend(closes, period=3, deadband_percent=0.05).direction is (
        TrendDirection.SIDEWAYS
    )
    assert horizon_trend(closes, period=3, deadband_percent=0.0).direction is (
        TrendDirection.UP
    )


def test_horizon_trend_lookback_capped_to_available_history():
    # 6 closes, period 3 -> 4 EMA points -> lookback = min(3, 3) = 3.
    trend = horizon_trend([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], period=3)
    assert trend.lookback == 3
    # 4 closes, period 3 -> 2 EMA points -> lookback capped to 1.
    short = horizon_trend([1.0, 2.0, 3.0, 4.0], period=3)
    assert short.lookback == 1


@pytest.mark.parametrize("kwargs", [{"period": 1}, {"deadband_percent": -0.1}])
def test_horizon_trend_rejects_bad_parameters(kwargs):
    with pytest.raises(ValueError):
        horizon_trend([1.0, 2.0, 3.0, 4.0], **{"period": 3, **kwargs})


# --------------------------- assess_trend (assembly over candles) ---------------------------


def test_assess_trend_rising_series_reads_uptrend():
    result = assess_trend(
        _candles([float(c) for c in range(10, 40)]), short_period=3, long_period=8
    )
    assert result.symbol == "AAPL"
    assert result.reference_price == 39.0
    assert result.short_term.direction is TrendDirection.UP
    assert result.long_term.direction is TrendDirection.UP
    assert result.reading is TrendReading.UPTREND


def test_assess_trend_falling_series_reads_downtrend():
    result = assess_trend(
        _candles([float(c) for c in range(40, 10, -1)]), short_period=3, long_period=8
    )
    assert result.reading is TrendReading.DOWNTREND


def test_assess_trend_long_up_short_down_reads_pullback():
    # A long steady climb, then a sharp recent drop: the long EMA is still rising
    # while the short EMA has rolled over — the "uptrend, pulling back" case.
    closes = [float(c) for c in range(10, 31)] + [28.0, 26.0, 24.0, 22.0]
    result = assess_trend(_candles(closes), short_period=5, long_period=20)
    assert result.long_term.direction is TrendDirection.UP
    assert result.short_term.direction is TrendDirection.DOWN
    assert result.reading is TrendReading.UPTREND_PULLBACK


def test_assess_trend_unknown_when_a_horizon_lacks_history():
    # 6 closes can warm the short (3) but not the long (50) EMA -> long is None.
    result = assess_trend(
        _candles([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]), short_period=3, long_period=50
    )
    assert result.short_term is not None
    assert result.long_term is None
    assert result.reading is TrendReading.UNKNOWN


def test_assess_trend_empty_series_is_graceful():
    result = assess_trend(
        CandleSeries(symbol="AAPL", timeframe=Timeframe.DAY_1, candles=()),
        short_period=3,
        long_period=8,
    )
    assert result.reference_price == 0.0
    assert result.short_term is None and result.long_term is None
    assert result.reading is TrendReading.UNKNOWN


@pytest.mark.parametrize(
    "kwargs",
    [
        {"short_period": 1, "long_period": 8},
        {"short_period": 8, "long_period": 8},  # short must be < long
        {"short_period": 20, "long_period": 5},  # short must be < long
    ],
)
def test_assess_trend_rejects_bad_periods(kwargs):
    with pytest.raises(ValueError):
        assess_trend(_candles([1.0, 2.0, 3.0, 4.0, 5.0]), **kwargs)
