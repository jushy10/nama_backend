"""Enterprise Business Rules: technical indicators derived from price history.

Pure calculations over close prices — no framework, no vendor, no I/O. An
indicator is a fact about a price series, so it lives in the domain next to the
Candle it's computed from. Outer layers fetch the candles (through a port) and
hand them here; nothing in this module reaches out for data.

Currently: RSI (Relative Strength Index, Wilder's original formulation) and
swing-low support levels.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
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


# --------------------------- Support levels (swing-low zones) ---------------------------

# Strength is read straight off how many separate swing lows formed a level: a
# price the market has repeatedly turned up from is stickier than a one-off dip.
_STRONG_MIN_TOUCHES = 3
_MODERATE_MIN_TOUCHES = 2


class SupportStrength(str, Enum):
    """How firmly a support level has held, by the number of swing lows that
    formed it. String values double as the API's JSON values (like RsiSignal)."""

    WEAK = "weak"  # a single swing low
    MODERATE = "moderate"  # two
    STRONG = "strong"  # three or more


@dataclass(frozen=True)
class SupportLevel:
    """One horizontal support level — a price zone where the stock has repeatedly
    found buyers.

    Built by clustering nearby *swing lows* (pivot lows): the level is the mean of
    the lows that formed it. ``touches`` is how many did (the strength signal),
    ``last_touched`` dates the most recent, and ``distance_percent`` is how far the
    level sits below the reference price it was measured against (``<= 0`` —
    support is at or below the current price).
    """

    price: float
    touches: int
    last_touched: date
    strength: SupportStrength
    distance_percent: float


@dataclass(frozen=True)
class SupportLevelSeries:
    """The support levels detected for one symbol at one timeframe.

    ``reference_price`` is the latest close the levels were measured against (what
    "below the current price" means here); ``levels`` are strongest-first and can
    be empty when there isn't enough history — or no swing low sits below the
    current price — to find any.
    """

    symbol: str
    timeframe: Timeframe
    reference_price: float
    levels: tuple[SupportLevel, ...]


def _strength_for(touches: int) -> SupportStrength:
    """Map a level's swing-low count onto its strength band."""
    if touches >= _STRONG_MIN_TOUCHES:
        return SupportStrength.STRONG
    if touches >= _MODERATE_MIN_TOUCHES:
        return SupportStrength.MODERATE
    return SupportStrength.WEAK


def _pivot_low_indices(lows: Sequence[float], window: int) -> list[int]:
    """Indices of the swing lows in ``lows`` — each a bar whose low is at or below
    every low within ``window`` bars on both sides (a *pivot low* / fractal).

    The first and last ``window`` bars are skipped: a swing low isn't confirmed
    until ``window`` bars have printed on each side, so the edges can't form one.
    A flat trough (consecutive equal lows) collapses to its first bar, so one
    V-shaped turn counts as a single touch rather than several.
    """
    n = len(lows)
    pivots: list[int] = []
    for i in range(window, n - window):
        low = lows[i]
        if low > min(lows[i - window : i + window + 1]):
            continue
        # A flat bottom flags every equal bar in the run; keep only the first.
        if pivots and i - pivots[-1] <= window and lows[pivots[-1]] == low:
            continue
        pivots.append(i)
    return pivots


def _is_taken_out(
    price: float,
    formed_at: datetime,
    closes: Sequence[float],
    timestamps: Sequence[datetime],
) -> bool:
    """True if a candle *after* ``formed_at`` closed below ``price`` — the level
    was broken and is no longer support.

    Only the close counts: a candle that wicked below the level but closed back
    above did not take it out. Only bars strictly after the level's most recent
    swing low count, so an earlier dip through the level that a later touch
    reclaimed (re-forming the support) is not treated as a break.
    """
    return any(
        ts > formed_at and close < price for close, ts in zip(closes, timestamps)
    )


def compute_support_levels(
    lows: Sequence[float],
    timestamps: Sequence[datetime],
    reference_price: float,
    *,
    closes: Sequence[float] | None = None,
    window: int = 5,
    tolerance: float = 0.02,
    max_levels: int = 5,
) -> list[SupportLevel]:
    """Detect horizontal support levels from a chronological (oldest-first) low
    series and the timestamps those lows fall on.

    Swing lows (``_pivot_low_indices``) are clustered by price — a low within
    ``tolerance`` (a fraction, e.g. ``0.02`` = 2%) above a cluster's base low joins
    it — and each cluster becomes a level at the mean of its lows. Only levels at
    or below ``reference_price`` (the latest close) are kept: support sits under
    the current price. Levels are ranked by strength (touch count, then recency),
    the top ``max_levels`` are taken, and they're returned nearest-first (highest
    price first — just under the quote).

    ``closes`` (index-aligned with ``lows``) drops **broken** levels: a support
    that a later candle *closed below* has been taken out and is no longer support
    (it flips to resistance), so it isn't returned. See ``_is_taken_out`` for the
    rule — closes only (a wick that pierced but closed back above does not break
    it), and only bars strictly after the level's most recent swing low, so a dip
    that a fresh touch later reclaimed still counts. When ``closes`` is omitted the
    break filter is skipped and levels are reported as-formed.

    Returns ``[]`` when there isn't enough history (fewer than ``2 * window + 1``
    lows), when no swing low sits at or below the price (or every candidate has
    been taken out), or when ``reference_price`` is non-positive — "couldn't find
    any" is not an error.

    Raises:
        ValueError: ``window < 2``, ``tolerance`` outside ``(0, 1)``,
            ``max_levels < 1``, or ``lows``/``timestamps``/``closes`` (when given)
            differ in length.
    """
    if window < 2:
        raise ValueError("window must be at least 2.")
    if not 0.0 < tolerance < 1.0:
        raise ValueError("tolerance must be between 0 and 1 (exclusive).")
    if max_levels < 1:
        raise ValueError("max_levels must be at least 1.")
    if len(lows) != len(timestamps):
        raise ValueError("lows and timestamps must be the same length.")
    if closes is not None and len(closes) != len(lows):
        raise ValueError("closes and lows must be the same length.")

    n = len(lows)
    if reference_price <= 0 or n < 2 * window + 1:
        return []

    pivots = _pivot_low_indices(lows, window)
    if not pivots:
        return []

    span_start, span_end = timestamps[0], timestamps[-1]
    span_seconds = (span_end - span_start).total_seconds() or 1.0

    # Agglomerate swing lows into zones: walk them in ascending price order and
    # keep extending a cluster while the next low is within `tolerance` of the
    # cluster's base (lowest) low — anchoring to the base bounds each zone's width
    # so a gentle up-drift can't chain unrelated lows into one runaway level.
    members = sorted(((lows[i], timestamps[i]) for i in pivots), key=lambda m: m[0])
    clusters: list[list[tuple[float, datetime]]] = []
    for price, ts in members:
        if clusters and (price - clusters[-1][0][0]) / clusters[-1][0][0] <= tolerance:
            clusters[-1].append((price, ts))
        else:
            clusters.append([(price, ts)])

    ranked: list[tuple[float, SupportLevel]] = []
    for cluster in clusters:
        price = round(sum(p for p, _ in cluster) / len(cluster), 2)
        if price > reference_price:  # at/above the quote — not support
            continue
        touches = len(cluster)
        latest_ts = max(ts for _, ts in cluster)
        # A later close below the level means it was taken out — drop it.
        if closes is not None and _is_taken_out(price, latest_ts, closes, timestamps):
            continue
        recency = (latest_ts - span_start).total_seconds() / span_seconds  # 0..1
        level = SupportLevel(
            price=price,
            touches=touches,
            last_touched=latest_ts.date(),
            strength=_strength_for(touches),
            distance_percent=round((price - reference_price) / reference_price * 100, 2),
        )
        # Touch count dominates; recency (0..1) breaks ties toward fresher levels.
        ranked.append((touches + recency, level))

    # Strongest first (nearest the price on a tie), keep the top N, then present
    # them nearest-support-first — the highest price, just under the quote.
    ranked.sort(key=lambda item: (item[0], item[1].distance_percent), reverse=True)
    top = [level for _, level in ranked[:max_levels]]
    top.sort(key=lambda level: level.price, reverse=True)
    return top


def support_levels(
    series: CandleSeries,
    *,
    window: int = 5,
    tolerance: float = 0.02,
    max_levels: int = 5,
) -> SupportLevelSeries:
    """Detect support levels for a candle series, measured against its latest close.

    Pure: the detection (``compute_support_levels``) runs on the candles' lows and
    their timestamps, with the final close as the reference price the levels sit
    below. The candles' closes are passed too, so a level a later candle closed
    below (taken out) is dropped. Given the same series it always returns the same
    result.
    """
    lows = [candle.low for candle in series.candles]
    closes = [candle.close for candle in series.candles]
    timestamps = [candle.timestamp for candle in series.candles]
    reference_price = series.candles[-1].close if series.candles else 0.0
    levels = compute_support_levels(
        lows,
        timestamps,
        reference_price,
        closes=closes,
        window=window,
        tolerance=tolerance,
        max_levels=max_levels,
    )
    return SupportLevelSeries(
        symbol=series.symbol,
        timeframe=series.timeframe,
        reference_price=round(reference_price, 2),
        levels=tuple(levels),
    )
