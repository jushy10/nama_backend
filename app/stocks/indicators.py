"""Enterprise Business Rules: technical indicators derived from price history.

Pure calculations over close prices — no framework, no vendor, no I/O. An
indicator is a fact about a price series, so it lives in the domain next to the
Candle it's computed from. Outer layers fetch the candles (through a port) and
hand them here; nothing in this module reaches out for data.

Currently: RSI (Relative Strength Index), Wilder's original formulation.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.stocks.entities import CandleSeries, Timeframe

# Wilder's conventional interpretation bands. An RSI at or above the overbought
# line is the classic "momentum is stretched — consider taking profit" zone;
# at or below the oversold line is the mirror image. These are descriptive
# thresholds, not trade advice.
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0


class RsiSignal(str, Enum):
    """Which interpretation band the latest RSI reading falls in.

    String values double as the API's JSON values. ``OVERBOUGHT`` is the
    take-profit-relevant band; the labels describe the reading, they do not
    instruct a trade.
    """

    OVERBOUGHT = "overbought"
    OVERSOLD = "oversold"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class RsiPoint:
    """One RSI value at the close it was computed for (timestamp is that bar's)."""

    timestamp: datetime
    value: float


@dataclass(frozen=True)
class RsiSeries:
    """RSI computed across a symbol's price history, oldest point first.

    The first ``period`` candles seed the initial average and carry no RSI, so
    ``points`` is shorter than the input series by ``period`` (and empty when
    there isn't enough history). ``latest``/``signal`` are convenience views of
    the final point — the end that matters for a take-profit read.
    """

    symbol: str
    timeframe: Timeframe
    period: int
    points: tuple[RsiPoint, ...]

    @property
    def latest(self) -> RsiPoint | None:
        """The most recent RSI point, or None when there wasn't enough history."""
        return self.points[-1] if self.points else None

    @property
    def signal(self) -> RsiSignal | None:
        """Interpretation band of the latest reading (None when no points)."""
        latest = self.latest
        if latest is None:
            return None
        if latest.value >= RSI_OVERBOUGHT:
            return RsiSignal.OVERBOUGHT
        if latest.value <= RSI_OVERSOLD:
            return RsiSignal.OVERSOLD
        return RsiSignal.NEUTRAL


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    """Map a smoothed average gain/loss pair onto the 0–100 RSI scale."""
    if avg_loss == 0:
        # No down moves in the window. Pure gains pin RSI to 100; a perfectly
        # flat window (no moves at all) is neither over- nor oversold -> 50.
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def compute_rsi(closes: Sequence[float], period: int = 14) -> list[float]:
    """Wilder's RSI over a chronological (oldest-first) close series.

    Returns one value per close from index ``period`` onward — the first
    ``period`` closes only seed the initial average. Returns ``[]`` when there
    isn't enough history (fewer than ``period + 1`` closes).

    Raises:
        ValueError: period < 2 (RSI needs at least one gain/loss pair).
    """
    if period < 2:
        raise ValueError("RSI period must be at least 2.")
    if len(closes) <= period:
        return []

    # Seed: simple average of the first `period` price changes.
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    values = [_rsi_from(avg_gain, avg_loss)]

    # Wilder smoothing for every later close.
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        values.append(_rsi_from(avg_gain, avg_loss))
    return values


def rsi_series(series: CandleSeries, period: int = 14) -> RsiSeries:
    """Compute RSI for a candle series, aligning each value to its close's bar.

    The math runs on close prices; timestamps come from the candles those
    values land on (``candles[period:]``), so each RsiPoint dates the bar it
    describes. Pure — given the same series it always returns the same result.
    """
    closes = [candle.close for candle in series.candles]
    values = compute_rsi(closes, period)
    points = tuple(
        RsiPoint(timestamp=candle.timestamp, value=value)
        for candle, value in zip(series.candles[period:], values)
    )
    return RsiSeries(
        symbol=series.symbol,
        timeframe=series.timeframe,
        period=period,
        points=points,
    )
