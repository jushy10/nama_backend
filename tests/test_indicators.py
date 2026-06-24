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
    compute_rsi,
    rsi_series,
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
