from datetime import datetime, timedelta, timezone

import pytest

from app.stocks.entities import Candle, CandleSeries, Timeframe
from app.stocks.company.charts.indicators import (
    HorizonTrend,
    IndicatorSpec,
    SupportStrength,
    TrendAssessment,
    TrendDirection,
    TrendReading,
    _combined_reading,
    _effective_direction,
    assess_trend,
    build_indicator,
    build_indicators,
    compute_adx,
    compute_atr,
    compute_bollinger,
    compute_cci,
    compute_ema,
    compute_macd,
    compute_mfi,
    compute_obv,
    compute_roc,
    compute_rsi,
    compute_sma,
    compute_stochastic,
    compute_support_levels,
    compute_vwap,
    compute_williams_r,
    ema_line,
    ema_series,
    horizon_trend,
    indicator_warmup_bars,
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


def test_horizon_trend_rising_series_effective_matches_slope():
    # Price leads a rising line, so slope and price agree -> effective is up too.
    trend = horizon_trend([float(c) for c in range(10, 25)], period=3)
    assert trend.direction is TrendDirection.UP
    assert trend.price_vs_ema_percent > 0
    assert trend.effective_direction is TrendDirection.UP


def test_horizon_trend_price_broken_below_rising_line_reads_down():
    # A long steady climb, then a sharp final break far below the (slow, lagging)
    # line: the slope over its lookback is still up, but price now sits well below
    # the EMA, and price leads -> the horizon has effectively turned down.
    closes = [float(c) for c in range(100, 260)]  # steady climb
    closes += [215.0, 205.0, 200.0]  # a decisive break below the line
    trend = horizon_trend(closes, period=50)
    assert trend.direction is TrendDirection.UP  # the line still slopes up
    assert trend.price_vs_ema_percent < -1.0  # price is below its own line
    assert trend.effective_direction is TrendDirection.DOWN


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


@pytest.mark.parametrize(
    "kwargs",
    [
        {"period": 1},
        {"deadband_percent": -0.1},
        {"price_deadband_percent": -0.1},
    ],
)
def test_horizon_trend_rejects_bad_parameters(kwargs):
    with pytest.raises(ValueError):
        horizon_trend([1.0, 2.0, 3.0, 4.0], **{"period": 3, **kwargs})


# --------------------------- assess_trend (assembly over candles) ---------------------------


def test_assess_trend_rising_series_reads_strong_uptrend():
    # All three horizons rising and aligned -> the strongest bullish read.
    result = assess_trend(
        _candles([float(c) for c in range(10, 40)]),
        short_period=3,
        medium_period=5,
        long_period=8,
    )
    assert result.symbol == "AAPL"
    assert result.reference_price == 39.0
    assert result.short_term.direction is TrendDirection.UP
    assert result.medium_term.direction is TrendDirection.UP
    assert result.long_term.direction is TrendDirection.UP
    assert result.reading is TrendReading.STRONG_UPTREND


def test_assess_trend_falling_series_reads_strong_downtrend():
    result = assess_trend(
        _candles([float(c) for c in range(40, 10, -1)]),
        short_period=3,
        medium_period=5,
        long_period=8,
    )
    assert result.reading is TrendReading.STRONG_DOWNTREND


def test_assess_trend_unknown_when_a_horizon_lacks_history():
    # 6 closes can warm the short (3) but not the long (50) EMA -> long is None.
    result = assess_trend(
        _candles([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
        short_period=3,
        medium_period=4,
        long_period=50,
    )
    assert result.short_term is not None
    assert result.long_term is None
    assert result.reading is TrendReading.UNKNOWN


def test_assess_trend_empty_series_is_graceful():
    result = assess_trend(
        CandleSeries(symbol="AAPL", timeframe=Timeframe.DAY_1, candles=()),
        short_period=3,
        medium_period=5,
        long_period=8,
    )
    assert result.reference_price == 0.0
    assert result.short_term is None
    assert result.medium_term is None
    assert result.long_term is None
    assert result.reading is TrendReading.UNKNOWN


@pytest.mark.parametrize(
    "kwargs",
    [
        {"short_period": 1, "medium_period": 4, "long_period": 8},  # period < 2
        {"short_period": 4, "medium_period": 4, "long_period": 8},  # not increasing
        {"short_period": 4, "medium_period": 8, "long_period": 8},  # not increasing
        {"short_period": 20, "medium_period": 10, "long_period": 5},  # descending
    ],
)
def test_assess_trend_rejects_bad_periods(kwargs):
    with pytest.raises(ValueError):
        assess_trend(_candles([1.0, 2.0, 3.0, 4.0, 5.0]), **kwargs)


# ------------------- _combined_reading (the 3-horizon classifier) -------------------

_UP = TrendDirection.UP
_DOWN = TrendDirection.DOWN
_FLAT = TrendDirection.SIDEWAYS


@pytest.mark.parametrize(
    "long_dir, medium_dir, short_dir, expected",
    [
        # Long up: medium is the main qualifier, short confirms strength.
        (_UP, _UP, _UP, TrendReading.STRONG_UPTREND),
        (_UP, _UP, _FLAT, TrendReading.UPTREND),
        (_UP, _FLAT, _UP, TrendReading.UPTREND),
        (_UP, _UP, _DOWN, TrendReading.UPTREND_PULLBACK),  # only near-term dip
        (_UP, _FLAT, _DOWN, TrendReading.UPTREND_PULLBACK),
        (_UP, _DOWN, _UP, TrendReading.UPTREND_WEAKENING),  # mid-term rolled over
        (_UP, _DOWN, _DOWN, TrendReading.UPTREND_WEAKENING),
        # Long down: the mirror image.
        (_DOWN, _DOWN, _DOWN, TrendReading.STRONG_DOWNTREND),
        (_DOWN, _DOWN, _FLAT, TrendReading.DOWNTREND),
        (_DOWN, _DOWN, _UP, TrendReading.DOWNTREND_BOUNCE),  # only near-term bounce
        (_DOWN, _FLAT, _UP, TrendReading.DOWNTREND_BOUNCE),
        (_DOWN, _UP, _DOWN, TrendReading.DOWNTREND_RECOVERING),  # mid-term turned up
        (_DOWN, _UP, _UP, TrendReading.DOWNTREND_RECOVERING),
        # Long flat: a range. Medium leads the break/turn; short breaks ties.
        (_FLAT, _FLAT, _FLAT, TrendReading.RANGE_BOUND),
        (_FLAT, _UP, _UP, TrendReading.RANGE_BREAKING_UP),
        (_FLAT, _DOWN, _DOWN, TrendReading.RANGE_BREAKING_DOWN),
        (_FLAT, _UP, _FLAT, TrendReading.RANGE_TURNING_UP),
        (_FLAT, _UP, _DOWN, TrendReading.RANGE_TURNING_UP),  # medium leads
        (_FLAT, _DOWN, _UP, TrendReading.RANGE_TURNING_DOWN),  # medium leads
        (_FLAT, _FLAT, _UP, TrendReading.RANGE_TURNING_UP),  # short breaks the tie
        (_FLAT, _FLAT, _DOWN, TrendReading.RANGE_TURNING_DOWN),
    ],
)
def test_combined_reading_taxonomy(long_dir, medium_dir, short_dir, expected):
    assert _combined_reading(long_dir, medium_dir, short_dir) is expected


# --------------- _effective_direction (slope folded with price's side) ---------------


@pytest.mark.parametrize(
    "slope_dir, price_vs_ema, expected",
    [
        # Slope and price agree -> that direction (a clean leg).
        (_UP, 5.0, _UP),
        (_DOWN, -5.0, _DOWN),
        # Price within the 1% band abstains -> the slope decides.
        (_UP, 0.5, _UP),
        (_DOWN, -0.5, _DOWN),
        (_UP, 1.0, _UP),  # boundary: exactly 1% is still "on the line"
        (_UP, -1.0, _UP),
        # Slope flat -> price breaks the tie.
        (_FLAT, 5.0, _UP),
        (_FLAT, -5.0, _DOWN),
        (_FLAT, 0.5, _FLAT),  # both flat
        # Conflict: price leads, because the slope is trailing and price is now.
        (_UP, -5.0, _DOWN),  # rising line, price broken below it (the chart's case)
        (_DOWN, 5.0, _UP),  # falling line, price jumped above it
    ],
)
def test_effective_direction(slope_dir, price_vs_ema, expected):
    assert _effective_direction(slope_dir, price_vs_ema, 1.0) is expected


def test_assess_trend_price_below_rising_line_diverges_from_slope():
    # The chart's case: a long climb then a late selloff off the highs. The medium
    # EMA's lookback still spans the advance, so its *line* slopes up — but price has
    # broken below it, and price leads, so its effective vote is down. The long line
    # is slow enough that price is still above it, so the primary uptrend stands and
    # the folded headline reads as weakening, not a clean uptrend.
    closes = [float(c) for c in range(100, 300)]  # long steady climb
    closes += [290.0, 282.0, 276.0, 272.0, 270.0]  # a drop off the highs
    result = assess_trend(
        _candles(closes), short_period=10, medium_period=30, long_period=100
    )
    # Medium: line up, price broken below it -> effective turns down.
    assert result.medium_term.direction is TrendDirection.UP
    assert result.medium_term.price_vs_ema_percent < -1.0
    assert result.medium_term.effective_direction is TrendDirection.DOWN
    # Short: price is below its fast line too -> down.
    assert result.short_term.effective_direction is TrendDirection.DOWN
    # Long: price is still above its slow line -> the primary uptrend holds.
    assert result.long_term.effective_direction is TrendDirection.UP
    assert result.reading is TrendReading.UPTREND_WEAKENING


def test_reading_is_built_from_effective_directions_not_raw_slope():
    # Deterministic proof that the fold reaches the headline: raw slopes that read a
    # clean UPTREND, but with price having broken below the faster lines the effective
    # directions read a PULLBACK.
    def _horizon(direction, effective):
        return HorizonTrend(
            period=1,
            lookback=1,
            direction=direction,
            effective_direction=effective,
            slope_percent=0.0,
            change_percent=0.0,
            price_vs_ema_percent=0.0,
            ema=0.0,
        )

    assessment = TrendAssessment(
        symbol="AAPL",
        timeframe=Timeframe.DAY_1,
        reference_price=0.0,
        long_term=_horizon(_UP, _UP),  # up and price above
        medium_term=_horizon(_UP, _FLAT),  # line up, price broke below -> flat
        short_term=_horizon(_FLAT, _DOWN),  # line flat, price below -> down
    )
    # Raw slopes alone (up, up, flat) would be a clean uptrend...
    assert _combined_reading(_UP, _UP, _FLAT) is TrendReading.UPTREND
    # ...but the reading folds price in and steps down to a pullback.
    assert assessment.reading is TrendReading.UPTREND_PULLBACK


def _ohlcv(bars: list[tuple], timeframe: Timeframe = Timeframe.DAY_1) -> CandleSeries:
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(
        Candle(
            timestamp=base + timedelta(days=i),
            open=bar[2],
            high=bar[0],
            low=bar[1],
            close=bar[2],
            volume=(bar[3] if len(bar) > 3 else 1000),
        )
        for i, bar in enumerate(bars)
    )
    return CandleSeries(symbol="AAPL", timeframe=timeframe, candles=candles)


def test_compute_sma_matches_hand_computation():
    # windows of 2: (2+4)/2, (4+6)/2, (6+8)/2
    assert compute_sma([2.0, 4.0, 6.0, 8.0], period=2) == [3.0, 5.0, 7.0]


def test_compute_sma_empty_when_not_enough_history():
    assert compute_sma([5.0], period=2) == []


def test_compute_rsi_all_gains_is_100():
    # A strictly rising series has no losses -> RSI pinned at 100.
    values = compute_rsi([float(x) for x in range(1, 20)], period=14)
    assert values == [100.0] * len(values)
    # First value lands `period` closes in -> n - period readings.
    assert len(values) == 19 - 14


def test_compute_rsi_all_losses_is_zero():
    values = compute_rsi([float(x) for x in range(20, 1, -1)], period=14)
    assert values == [0.0] * len(values)


def test_compute_rsi_flat_series_is_neutral_50():
    values = compute_rsi([50.0] * 20, period=14)
    assert values == [50.0] * len(values)


def test_compute_rsi_empty_when_not_enough_history():
    assert compute_rsi([1.0, 2.0, 3.0], period=14) == []


def test_compute_macd_rising_series_is_positive_and_aligned():
    closes = [float(x) for x in range(1, 60)]
    macd_line, signal_line, histogram = compute_macd(closes)  # 12/26/9
    assert macd_line and signal_line and histogram
    assert macd_line[-1] > 0  # fast EMA above slow on a climb
    # histogram is macd - signal over the signal's tail, so they share a length.
    assert len(histogram) == len(signal_line)


def test_compute_macd_empty_when_history_too_short():
    assert compute_macd([1.0, 2.0, 3.0]) == ([], [], [])


def test_compute_macd_rejects_fast_not_shorter_than_slow():
    with pytest.raises(ValueError):
        compute_macd([1.0, 2.0, 3.0], fast=26, slow=12)


def test_compute_bollinger_flat_series_collapses_the_bands():
    upper, middle, lower = compute_bollinger([5.0] * 5, period=3)
    assert middle == [5.0, 5.0, 5.0]
    assert upper == middle == lower  # zero deviation -> bands sit on the mean


def test_compute_bollinger_orders_bands_and_centres_on_the_sma():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    upper, middle, lower = compute_bollinger(closes, period=3, num_std=2)
    assert middle == compute_sma(closes, 3)  # centre line is the SMA
    assert all(u > m > lo for u, m, lo in zip(upper, middle, lower))


def test_compute_atr_matches_hand_computation():
    highs = [10.0, 12.0, 11.0, 13.0]
    lows = [8.0, 9.0, 9.0, 10.0]
    closes = [9.0, 11.0, 10.0, 12.0]
    # TR = [3, 2, 3]; seed = mean(3,2) = 2.5; next = (2.5*1 + 3)/2 = 2.75.
    assert compute_atr(highs, lows, closes, period=2) == [2.5, 2.75]


def test_compute_atr_empty_when_history_too_short():
    assert compute_atr([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], period=14) == []


def test_compute_stochastic_close_at_range_top_reads_high():
    # Close pinned to the high of a rising range -> %K near 100.
    bars = [(float(h), float(h) - 5, float(h)) for h in range(10, 30)]
    highs = [b[0] for b in bars]
    lows = [b[1] for b in bars]
    closes = [b[2] for b in bars]
    k_line, d_line = compute_stochastic(highs, lows, closes, k_period=5)
    assert k_line and d_line
    assert k_line[-1] == 100.0  # last close is the period high
    assert all(0.0 <= v <= 100.0 for v in k_line)


def test_compute_adx_uptrend_has_plus_di_above_minus_di():
    bars = [(float(h) + 1, float(h) - 1, float(h)) for h in range(10, 50)]
    highs = [b[0] for b in bars]
    lows = [b[1] for b in bars]
    closes = [b[2] for b in bars]
    adx, plus_di, minus_di = compute_adx(highs, lows, closes, period=14)
    assert plus_di and minus_di and adx
    assert plus_di[-1] > minus_di[-1]  # a clean climb: upward pressure dominates


def test_compute_obv_matches_hand_computation():
    closes = [10.0, 11.0, 10.0, 12.0]
    volumes = [100, 200, 300, 400]
    # 0, +200 (up), -300 (down), +400 (up)
    assert compute_obv(closes, volumes) == [0.0, 200.0, -100.0, 300.0]


def test_compute_obv_treats_missing_volume_as_zero():
    assert compute_obv([10.0, 11.0], [None, None]) == [0.0, 0.0]


def test_compute_vwap_is_the_running_volume_weighted_average():
    # Equal volumes -> cumulative mean of the typical prices (h=l=c here).
    assert compute_vwap([10.0, 20.0], [10.0, 20.0], [10.0, 20.0], [10, 10]) == [10.0, 15.0]


def test_compute_williams_r_close_at_low_is_minus_100():
    # Close at the bottom of the range -> %R = -100.
    values = compute_williams_r([10.0, 10.0], [5.0, 5.0], [7.0, 5.0], period=2)
    assert values == [-100.0]


def test_compute_cci_flat_series_is_zero():
    flat = [5.0] * 5
    assert compute_cci(flat, flat, flat, period=3) == [0.0, 0.0, 0.0]


def test_compute_roc_matches_hand_computation():
    # 100*(12-10)/10 = 20; 100*(13-11)/11 ~= 18.1818; 100*(14-12)/12 ~= 16.6667.
    # compute_* return full precision; the 4dp rounding happens at the line boundary.
    result = compute_roc([10.0, 11.0, 12.0, 13.0, 14.0], period=2)
    assert result == pytest.approx([20.0, 100 * 2 / 11, 100 * 2 / 12])


def test_compute_mfi_all_positive_flow_is_100():
    # Strictly rising typical price -> no negative money flow -> MFI pinned at 100.
    bars = [(float(h), float(h) - 1, float(h)) for h in range(10, 25)]
    highs = [b[0] for b in bars]
    lows = [b[1] for b in bars]
    closes = [b[2] for b in bars]
    volumes = [1000] * len(bars)
    values = compute_mfi(highs, lows, closes, volumes, period=5)
    assert values == [100.0] * len(values)


# --------------------------- build_indicator (candles -> Indicator) ---------------------------


def test_build_indicator_rsi_shape_and_tail_alignment():
    series = _candles([float(x) for x in range(1, 40)])
    indicator = build_indicator(series, IndicatorSpec(name="rsi"))
    assert indicator.name == "rsi"
    assert indicator.label == "RSI (14)"
    assert indicator.overlay is False
    assert [line.key for line in indicator.lines] == ["rsi"]
    line = indicator.lines[0]
    # Tail-aligned: the final reading dates the final candle.
    assert line.points[-1].timestamp == series.candles[-1].timestamp
    assert line.latest.value == 100.0  # strictly rising -> RSI 100


def test_build_indicator_macd_has_three_lines():
    series = _candles([float(x) for x in range(1, 60)])
    indicator = build_indicator(series, IndicatorSpec(name="macd"))
    assert indicator.label == "MACD (12/26/9)"
    assert [line.key for line in indicator.lines] == ["macd", "signal", "histogram"]


def test_build_indicator_overlay_flags():
    series = _candles([float(x) for x in range(1, 30)])
    assert build_indicator(series, IndicatorSpec("sma", 10)).overlay is True
    assert build_indicator(series, IndicatorSpec("vwap")).overlay is True
    assert build_indicator(series, IndicatorSpec("rsi")).overlay is False


def test_build_indicator_period_override_shows_in_label():
    series = _candles([float(x) for x in range(1, 30)])
    assert build_indicator(series, IndicatorSpec("sma", 20)).label == "SMA (20)"


def test_build_indicator_rejects_unknown_name():
    with pytest.raises(ValueError):
        build_indicator(_candles([1.0, 2.0, 3.0]), IndicatorSpec("bogus"))


def test_build_indicator_rejects_period_on_a_no_period_indicator():
    with pytest.raises(ValueError):
        build_indicator(_candles([1.0, 2.0, 3.0]), IndicatorSpec("macd", 5))


def test_build_indicator_rejects_period_below_two():
    with pytest.raises(ValueError):
        build_indicator(_candles([1.0, 2.0, 3.0]), IndicatorSpec("rsi", 1))


def test_build_indicators_preserves_request_order():
    series = _candles([float(x) for x in range(1, 40)])
    result = build_indicators(series, [IndicatorSpec("macd"), IndicatorSpec("rsi")])
    assert result.symbol == "AAPL"
    assert [ind.name for ind in result.indicators] == ["macd", "rsi"]


@pytest.mark.parametrize(
    "name,period,expected",
    [
        ("rsi", None, 14),
        ("rsi", 21, 21),
        ("macd", None, 35),  # slow (26) + signal (9)
        ("adx", None, 28),  # 2 x period
        ("adx", 10, 20),
        ("stoch", None, 20),  # period (14) + smooth (3) + signal (3)
        ("sma", 200, 200),
        ("obv", None, 0),
        ("vwap", None, 0),
    ],
)
def test_indicator_warmup_bars(name, period, expected):
    assert indicator_warmup_bars(name, period) == expected
