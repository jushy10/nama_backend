"""Enterprise Business Rules: technical indicators derived from price history.

Pure calculations over close prices — no framework, no vendor, no I/O. An
indicator is a fact about a price series, so it lives in the domain next to the
Candle it's computed from. Outer layers fetch the candles (through a port) and
hand them here; nothing in this module reaches out for data.

Currently: EMA (exponential moving average — e.g. the 9/21/50 chart overlay)
and swing-low support levels.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from app.stocks.entities import CandleSeries, Timeframe


# --------------------------- EMA (exponential moving average) ---------------------------


@dataclass(frozen=True)
class EmaPoint:
    """One EMA value at the close it was computed for (timestamp is that bar's).

    An EMA rides on the price scale, so ``value`` is in the quote currency — an
    overlay drawn straight on the candle chart's price axis.
    """

    timestamp: datetime
    value: float


@dataclass(frozen=True)
class EmaLine:
    """One EMA overlay line at a single period (e.g. the 50-EMA).

    Seeded from the simple average of the first ``period`` closes, so ``points``
    is shorter than the input series by ``period - 1`` (and empty when there
    isn't enough history). ``latest`` is a convenience view of the final point.
    """

    period: int
    points: tuple[EmaPoint, ...]

    @property
    def latest(self) -> EmaPoint | None:
        """The most recent EMA point, or None when there wasn't enough history."""
        return self.points[-1] if self.points else None


@dataclass(frozen=True)
class EmaSeries:
    """One or more EMA lines for a symbol at one timeframe, one per requested
    period — e.g. the 9/21/50 overlay drawn on a single chart.

    ``lines`` preserves the order the periods were requested in.
    """

    symbol: str
    timeframe: Timeframe
    lines: tuple[EmaLine, ...]


def compute_ema(closes: Sequence[float], period: int) -> list[float]:
    """Exponential moving average over a chronological (oldest-first) close series.

    Seeded with the simple average of the first ``period`` closes (the
    conventional seed), then smoothed with multiplier ``k = 2 / (period + 1)``:
    each later value weights the newest close by ``k`` and carries the rest from
    the prior EMA. Returns one value per close from index ``period - 1`` onward —
    the first ``period - 1`` closes only seed the initial average — so the result
    has ``len(closes) - period + 1`` values. Returns ``[]`` when there isn't
    enough history (fewer than ``period`` closes).

    Raises:
        ValueError: period < 1.
    """
    if period < 1:
        raise ValueError("EMA period must be at least 1.")
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period  # SMA seed
    values = [round(ema, 4)]
    for close in closes[period:]:
        ema = close * k + ema * (1 - k)
        values.append(round(ema, 4))
    return values


def ema_line(series: CandleSeries, period: int) -> EmaLine:
    """Compute one EMA line for a candle series, aligning each value to its close's
    bar.

    The math runs on close prices; timestamps come from the candles those values
    land on (``candles[period - 1:]``, since the seed consumes the first
    ``period`` closes and its value dates the last of them). Pure.
    """
    closes = [candle.close for candle in series.candles]
    values = compute_ema(closes, period)
    points = tuple(
        EmaPoint(timestamp=candle.timestamp, value=value)
        for candle, value in zip(series.candles[period - 1 :], values)
    )
    return EmaLine(period=period, points=points)


def ema_series(series: CandleSeries, periods: Sequence[int]) -> EmaSeries:
    """Compute an EMA overlay (one line per period) for a candle series.

    Each period is computed independently over the same closes; ``lines`` keeps
    the caller's period order. Pure — given the same series it always returns the
    same result.
    """
    return EmaSeries(
        symbol=series.symbol,
        timeframe=series.timeframe,
        lines=tuple(ema_line(series, period) for period in periods),
    )


# --------------------------- Support levels (swing-low zones) ---------------------------

# Strength is read straight off how many separate swing lows formed a level: a
# price the market has repeatedly turned up from is stickier than a one-off dip.
_STRONG_MIN_TOUCHES = 3
_MODERATE_MIN_TOUCHES = 2


class SupportStrength(str, Enum):
    """How firmly a support level has held, by the number of swing lows that
    formed it. String values double as the API's JSON values."""

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


# --------------------------- Trend (multi-horizon direction) ---------------------------

# A near-flat EMA shouldn't read as a trend: anything drifting slower than this
# many percent *per bar* is called SIDEWAYS rather than flip-flopping UP/DOWN on
# noise. 0.05%/bar is ~1% over a 20-bar horizon (~12% annualized on daily bars) —
# a gentle but real slope clears it; chop doesn't.
_DEFAULT_FLAT_THRESHOLD_PERCENT = 0.05


class TrendDirection(str, Enum):
    """Which way one horizon is heading. String values double as the API's JSON
    values."""

    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


class TrendReading(str, Enum):
    """The short- and long-term horizons combined into one plain reading — the
    whole point of the endpoint (e.g. "long-term up but short-term down"). The long
    horizon sets the primary trend; the short one qualifies it. String values
    double as the API's JSON values."""

    UPTREND = "uptrend"  # both up
    UPTREND_PULLBACK = "uptrend_pullback"  # long up, short down
    UPTREND_CONSOLIDATING = "uptrend_consolidating"  # long up, short flat
    DOWNTREND = "downtrend"  # both down
    DOWNTREND_BOUNCE = "downtrend_bounce"  # long down, short up
    DOWNTREND_STALLING = "downtrend_stalling"  # long down, short flat
    RANGE_BOUND = "range_bound"  # both flat
    RANGE_TURNING_UP = "range_turning_up"  # long flat, short up
    RANGE_TURNING_DOWN = "range_turning_down"  # long flat, short down
    UNKNOWN = "unknown"  # not enough history on one or both horizons


# The 3×3 map from (long direction, short direction) to the combined reading.
_READINGS: dict[tuple[TrendDirection, TrendDirection], TrendReading] = {
    (TrendDirection.UP, TrendDirection.UP): TrendReading.UPTREND,
    (TrendDirection.UP, TrendDirection.DOWN): TrendReading.UPTREND_PULLBACK,
    (TrendDirection.UP, TrendDirection.SIDEWAYS): TrendReading.UPTREND_CONSOLIDATING,
    (TrendDirection.DOWN, TrendDirection.DOWN): TrendReading.DOWNTREND,
    (TrendDirection.DOWN, TrendDirection.UP): TrendReading.DOWNTREND_BOUNCE,
    (TrendDirection.DOWN, TrendDirection.SIDEWAYS): TrendReading.DOWNTREND_STALLING,
    (TrendDirection.SIDEWAYS, TrendDirection.UP): TrendReading.RANGE_TURNING_UP,
    (TrendDirection.SIDEWAYS, TrendDirection.DOWN): TrendReading.RANGE_TURNING_DOWN,
    (TrendDirection.SIDEWAYS, TrendDirection.SIDEWAYS): TrendReading.RANGE_BOUND,
}


@dataclass(frozen=True)
class HorizonTrend:
    """One horizon's trend read, taken from the slope of its EMA.

    The direction is read off the EMA's *slope* — the smoothed price line's heading
    over its own timescale — not the raw closes, so a single noisy bar can't flip
    it. ``slope_percent`` is that slope as an average percent change *per bar* (the
    figure the SIDEWAYS deadband is applied to); ``change_percent`` is the same move
    totalled across the ``lookback`` bars it was measured over (the human-readable
    "the trend line is up X%"). ``price_vs_ema_percent`` says where the latest close
    sits relative to the EMA — above (positive) or below — as context the direction
    deliberately does *not* fold in.
    """

    period: int
    lookback: int
    direction: TrendDirection
    slope_percent: float
    change_percent: float
    price_vs_ema_percent: float
    ema: float


@dataclass(frozen=True)
class TrendAssessment:
    """A stock's trend at two horizons plus their combined reading.

    ``short_term`` / ``long_term`` are each ``None`` when there isn't enough history
    to warm that horizon's EMA and measure its slope (a young listing, or a deep
    ``long_period`` over a short window). ``reference_price`` is the latest close the
    read was taken at.
    """

    symbol: str
    timeframe: Timeframe
    reference_price: float
    short_term: HorizonTrend | None
    long_term: HorizonTrend | None

    @property
    def reading(self) -> TrendReading:
        """The two horizons combined into one plain reading (the headline). The long
        horizon sets the primary trend; the short one qualifies it (aligned, pulling
        back, bouncing, …). ``UNKNOWN`` when either horizon is missing."""
        if self.long_term is None or self.short_term is None:
            return TrendReading.UNKNOWN
        return _READINGS[(self.long_term.direction, self.short_term.direction)]


def _classify_direction(slope_percent_per_bar: float, deadband: float) -> TrendDirection:
    """Map a per-bar EMA slope onto a direction, with a flat band around zero so a
    barely-moving line reads SIDEWAYS rather than a weak UP/DOWN."""
    if slope_percent_per_bar > deadband:
        return TrendDirection.UP
    if slope_percent_per_bar < -deadband:
        return TrendDirection.DOWN
    return TrendDirection.SIDEWAYS


def horizon_trend(
    closes: Sequence[float],
    period: int,
    *,
    deadband_percent: float = _DEFAULT_FLAT_THRESHOLD_PERCENT,
) -> HorizonTrend | None:
    """Read one horizon's trend from a chronological (oldest-first) close series.

    Smooths the closes into an EMA of ``period`` bars, then measures that line's
    slope from ``lookback = min(period, len(ema) - 1)`` bars ago to its latest
    value. The per-bar slope (percent) is classified UP / DOWN / SIDEWAYS against a
    ``deadband_percent`` flat band, so a gently drifting or choppy market reads
    SIDEWAYS rather than as a weak trend. Returns ``None`` when there isn't enough
    history to form at least two EMA points — nothing to measure a slope from.

    Raises:
        ValueError: ``period < 2`` or ``deadband_percent < 0``.
    """
    if period < 2:
        raise ValueError("trend period must be at least 2.")
    if deadband_percent < 0:
        raise ValueError("deadband_percent must be non-negative.")
    ema = compute_ema(closes, period)
    if len(ema) < 2:
        return None
    lookback = min(period, len(ema) - 1)
    now = ema[-1]
    prior = ema[-1 - lookback]
    change_percent = (now - prior) / prior * 100 if prior else 0.0
    slope_percent = change_percent / lookback
    price = closes[-1]
    price_vs_ema = (price - now) / now * 100 if now else 0.0
    return HorizonTrend(
        period=period,
        lookback=lookback,
        direction=_classify_direction(slope_percent, deadband_percent),
        slope_percent=round(slope_percent, 4),
        change_percent=round(change_percent, 2),
        price_vs_ema_percent=round(price_vs_ema, 2),
        ema=round(now, 2),
    )


def assess_trend(
    series: CandleSeries,
    *,
    short_period: int = 20,
    long_period: int = 50,
    deadband_percent: float = _DEFAULT_FLAT_THRESHOLD_PERCENT,
) -> TrendAssessment:
    """Assess a candle series' trend at a short and a long horizon.

    Both horizons are read from the same closes via ``horizon_trend`` (the slope of
    a short and a long EMA); their directions combine into ``TrendAssessment.reading``
    — the "long-term up but short-term down" headline. A horizon with too little
    history to warm its EMA comes back ``None`` (and the reading is ``UNKNOWN``).
    Pure: given the same series it always returns the same result.

    Raises:
        ValueError: ``short_period`` or ``long_period`` below 2,
            ``short_period >= long_period``, or ``deadband_percent < 0``.
    """
    if short_period < 2 or long_period < 2:
        raise ValueError("trend periods must be at least 2.")
    if short_period >= long_period:
        raise ValueError("short_period must be less than long_period.")
    closes = [candle.close for candle in series.candles]
    reference_price = closes[-1] if closes else 0.0
    return TrendAssessment(
        symbol=series.symbol,
        timeframe=series.timeframe,
        reference_price=round(reference_price, 2),
        short_term=horizon_trend(closes, short_period, deadband_percent=deadband_percent),
        long_term=horizon_trend(closes, long_period, deadband_percent=deadband_percent),
    )
