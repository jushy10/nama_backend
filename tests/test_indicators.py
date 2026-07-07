"""Unit tests for the pure RSI indicator (no I/O, no framework).

RSI is enterprise business logic, so it's tested directly on close prices and
candle series. Values here are hand-verifiable: monotone runs pin RSI to its
extremes, a flat run sits at the neutral midpoint, and a small mixed run is
worked out by hand against Wilder's smoothing.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.stocks.entities import Candle, CandleSeries, Timeframe
from app.stocks.indicators import (
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RsiPoint,
    RsiSeries,
    RsiSignal,
    SupportStrength,
    compute_rsi,
    compute_support_levels,
    rsi_series,
    support_levels,
)

# --------------------------- compute_rsi (pure math) ---------------------------


def test_compute_rsi_rejects_period_below_two():
    with pytest.raises(ValueError):
        compute_rsi([1.0, 2.0, 3.0], period=1)


def test_compute_rsi_empty_when_not_enough_history():
    # Need at least period + 1 closes to produce a single value.
    assert compute_rsi([10.0, 11.0], period=2) == []
    assert compute_rsi([10.0, 11.0, 12.0], period=3) == []


def test_compute_rsi_all_gains_pins_to_100():
    assert compute_rsi([1.0, 2.0, 3.0, 4.0, 5.0], period=2) == [100.0, 100.0, 100.0]


def test_compute_rsi_all_losses_pins_to_zero():
    assert compute_rsi([5.0, 4.0, 3.0, 2.0, 1.0], period=2) == [0.0, 0.0, 0.0]


def test_compute_rsi_flat_series_is_neutral_midpoint():
    # No movement either way -> avg_gain and avg_loss both 0 -> 50, not 100.
    assert compute_rsi([7.0, 7.0, 7.0, 7.0], period=2) == [50.0, 50.0]


def test_compute_rsi_mixed_series_matches_hand_computation():
    # closes [10,11,10,11], period 2:
    #   seed changes (+1,-1): avg_gain=0.5, avg_loss=0.5 -> RS 1   -> 50.0
    #   next change (+1):      avg_gain=0.75, avg_loss=0.25 -> RS 3 -> 75.0
    assert compute_rsi([10.0, 11.0, 10.0, 11.0], period=2) == [50.0, 75.0]


# --------------------------- rsi_series (assembly over candles) ---------------------------


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


def test_rsi_series_aligns_values_to_their_candle():
    series = _candles([10.0, 11.0, 10.0, 11.0])
    result = rsi_series(series, period=2)
    # The first `period` candles seed the average and carry no point.
    assert [p.value for p in result.points] == [50.0, 75.0]
    assert [p.timestamp for p in result.points] == [
        series.candles[2].timestamp,
        series.candles[3].timestamp,
    ]


def test_rsi_series_passes_through_symbol_timeframe_period():
    result = rsi_series(_candles([1.0, 2.0, 3.0, 4.0], Timeframe.HOUR_1), period=2)
    assert result.symbol == "AAPL"
    assert result.timeframe is Timeframe.HOUR_1
    assert result.period == 2


def test_rsi_series_empty_points_when_history_too_short():
    result = rsi_series(_candles([10.0, 11.0]), period=14)
    assert result.points == ()
    assert result.latest is None
    assert result.signal is None


# --------------------------- signal bands ---------------------------


def _series_ending_at(value: float) -> RsiSeries:
    return RsiSeries(
        symbol="AAPL",
        timeframe=Timeframe.DAY_1,
        period=14,
        points=(RsiPoint(timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc), value=value),),
    )


def test_latest_is_final_point():
    series = rsi_series(_candles([10.0, 11.0, 10.0, 11.0]), period=2)
    assert series.latest.value == 75.0


@pytest.mark.parametrize(
    "value, expected",
    [
        (RSI_OVERBOUGHT, RsiSignal.OVERBOUGHT),   # boundary is inclusive
        (85.0, RsiSignal.OVERBOUGHT),
        (RSI_OVERBOUGHT - 0.01, RsiSignal.NEUTRAL),
        (50.0, RsiSignal.NEUTRAL),
        (RSI_OVERSOLD + 0.01, RsiSignal.NEUTRAL),
        (RSI_OVERSOLD, RsiSignal.OVERSOLD),        # boundary is inclusive
        (15.0, RsiSignal.OVERSOLD),
    ],
)
def test_signal_bands(value, expected):
    assert _series_ending_at(value).signal is expected


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
