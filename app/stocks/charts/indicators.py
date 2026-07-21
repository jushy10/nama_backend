import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from app.stocks.entities import CandleSeries, Timeframe


# --------------------------- EMA (exponential moving average) ---------------------------


@dataclass(frozen=True)
class EmaPoint:
    timestamp: datetime
    value: float


@dataclass(frozen=True)
class EmaLine:
    period: int
    points: tuple[EmaPoint, ...]

    @property
    def latest(self) -> EmaPoint | None:
        return self.points[-1] if self.points else None


@dataclass(frozen=True)
class EmaSeries:
    symbol: str
    timeframe: Timeframe
    lines: tuple[EmaLine, ...]


def compute_ema(closes: Sequence[float], period: int) -> list[float]:
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
    closes = [candle.close for candle in series.candles]
    values = compute_ema(closes, period)
    points = tuple(
        EmaPoint(timestamp=candle.timestamp, value=value)
        for candle, value in zip(series.candles[period - 1 :], values)
    )
    return EmaLine(period=period, points=points)


def ema_series(series: CandleSeries, periods: Sequence[int]) -> EmaSeries:
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
    WEAK = "weak"  # a single swing low
    MODERATE = "moderate"  # two
    STRONG = "strong"  # three or more


@dataclass(frozen=True)
class SupportLevel:
    price: float
    touches: int
    last_touched: date
    strength: SupportStrength
    distance_percent: float


@dataclass(frozen=True)
class SupportLevelSeries:
    symbol: str
    timeframe: Timeframe
    reference_price: float
    levels: tuple[SupportLevel, ...]


def _strength_for(touches: int) -> SupportStrength:
    if touches >= _STRONG_MIN_TOUCHES:
        return SupportStrength.STRONG
    if touches >= _MODERATE_MIN_TOUCHES:
        return SupportStrength.MODERATE
    return SupportStrength.WEAK


def _pivot_low_indices(lows: Sequence[float], window: int) -> list[int]:
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

# How far (percent) the latest close must sit from a horizon's EMA before its
# position takes over the horizon's effective direction. Within this band the price
# is "on the line" and only the slope speaks; beyond it, price's side of the line
# *leads* (see ``_effective_direction``). Wider than the slope deadband because a
# price hugging its EMA whipsaws across it — 1% keeps a routine touch from flipping
# the read while a decisive break (the chart's 17-22% breach) clears it easily.
_DEFAULT_PRICE_FLAT_THRESHOLD_PERCENT = 1.0


class TrendDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


class TrendReading(str, Enum):
    STRONG_UPTREND = "strong_uptrend"  # long up, medium up, short up (all aligned)
    UPTREND = "uptrend"  # long up, faster horizons mildly confirming
    UPTREND_PULLBACK = "uptrend_pullback"  # long up, medium not down, short down
    UPTREND_WEAKENING = "uptrend_weakening"  # long up but medium down (mid-term rolling over)
    STRONG_DOWNTREND = "strong_downtrend"  # long down, medium down, short down
    DOWNTREND = "downtrend"  # long down, faster horizons mildly confirming
    DOWNTREND_BOUNCE = "downtrend_bounce"  # long down, medium not up, short up
    DOWNTREND_RECOVERING = "downtrend_recovering"  # long down but medium up (mid-term turning up)
    RANGE_BOUND = "range_bound"  # long flat, no clear tilt on the faster horizons
    RANGE_BREAKING_UP = "range_breaking_up"  # long flat, medium + short both up
    RANGE_BREAKING_DOWN = "range_breaking_down"  # long flat, medium + short both down
    RANGE_TURNING_UP = "range_turning_up"  # long flat, upward tilt on the faster horizons
    RANGE_TURNING_DOWN = "range_turning_down"  # long flat, downward tilt on the faster horizons
    UNKNOWN = "unknown"  # not enough history on one or more horizons


def _combined_reading(
    long_dir: TrendDirection,
    medium_dir: TrendDirection,
    short_dir: TrendDirection,
) -> TrendReading:
    up, down = TrendDirection.UP, TrendDirection.DOWN
    if long_dir is up:
        if medium_dir is down:
            return TrendReading.UPTREND_WEAKENING
        if medium_dir is up and short_dir is up:
            return TrendReading.STRONG_UPTREND
        if short_dir is down:
            return TrendReading.UPTREND_PULLBACK
        return TrendReading.UPTREND
    if long_dir is down:
        if medium_dir is up:
            return TrendReading.DOWNTREND_RECOVERING
        if medium_dir is down and short_dir is down:
            return TrendReading.STRONG_DOWNTREND
        if short_dir is up:
            return TrendReading.DOWNTREND_BOUNCE
        return TrendReading.DOWNTREND
    # long_dir is SIDEWAYS — a range; medium leads the break/turn, short breaks ties.
    if medium_dir is up and short_dir is up:
        return TrendReading.RANGE_BREAKING_UP
    if medium_dir is down and short_dir is down:
        return TrendReading.RANGE_BREAKING_DOWN
    if medium_dir is up or (medium_dir is not down and short_dir is up):
        return TrendReading.RANGE_TURNING_UP
    if medium_dir is down or (medium_dir is not up and short_dir is down):
        return TrendReading.RANGE_TURNING_DOWN
    return TrendReading.RANGE_BOUND


@dataclass(frozen=True)
class HorizonTrend:
    period: int
    lookback: int
    direction: TrendDirection
    effective_direction: TrendDirection
    slope_percent: float
    change_percent: float
    price_vs_ema_percent: float
    ema: float


@dataclass(frozen=True)
class TrendAssessment:
    symbol: str
    timeframe: Timeframe
    reference_price: float
    short_term: HorizonTrend | None
    medium_term: HorizonTrend | None
    long_term: HorizonTrend | None

    @property
    def reading(self) -> TrendReading:
        if (
            self.long_term is None
            or self.medium_term is None
            or self.short_term is None
        ):
            return TrendReading.UNKNOWN
        return _combined_reading(
            self.long_term.effective_direction,
            self.medium_term.effective_direction,
            self.short_term.effective_direction,
        )


def _classify_direction(slope_percent_per_bar: float, deadband: float) -> TrendDirection:
    if slope_percent_per_bar > deadband:
        return TrendDirection.UP
    if slope_percent_per_bar < -deadband:
        return TrendDirection.DOWN
    return TrendDirection.SIDEWAYS


def _effective_direction(
    slope_direction: TrendDirection,
    price_vs_ema_percent: float,
    price_deadband: float,
) -> TrendDirection:
    if price_vs_ema_percent > price_deadband:
        return TrendDirection.UP
    if price_vs_ema_percent < -price_deadband:
        return TrendDirection.DOWN
    return slope_direction


def horizon_trend(
    closes: Sequence[float],
    period: int,
    *,
    deadband_percent: float = _DEFAULT_FLAT_THRESHOLD_PERCENT,
    price_deadband_percent: float = _DEFAULT_PRICE_FLAT_THRESHOLD_PERCENT,
) -> HorizonTrend | None:
    if period < 2:
        raise ValueError("trend period must be at least 2.")
    if deadband_percent < 0:
        raise ValueError("deadband_percent must be non-negative.")
    if price_deadband_percent < 0:
        raise ValueError("price_deadband_percent must be non-negative.")
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
    slope_direction = _classify_direction(slope_percent, deadband_percent)
    return HorizonTrend(
        period=period,
        lookback=lookback,
        direction=slope_direction,
        effective_direction=_effective_direction(
            slope_direction, price_vs_ema, price_deadband_percent
        ),
        slope_percent=round(slope_percent, 4),
        change_percent=round(change_percent, 2),
        price_vs_ema_percent=round(price_vs_ema, 2),
        ema=round(now, 2),
    )


def assess_trend(
    series: CandleSeries,
    *,
    short_period: int = 20,
    medium_period: int = 50,
    long_period: int = 200,
    deadband_percent: float = _DEFAULT_FLAT_THRESHOLD_PERCENT,
    price_deadband_percent: float = _DEFAULT_PRICE_FLAT_THRESHOLD_PERCENT,
) -> TrendAssessment:
    if short_period < 2 or medium_period < 2 or long_period < 2:
        raise ValueError("trend periods must be at least 2.")
    if not short_period < medium_period < long_period:
        raise ValueError(
            "trend periods must be strictly increasing: "
            "short_period < medium_period < long_period."
        )
    closes = [candle.close for candle in series.candles]
    reference_price = closes[-1] if closes else 0.0

    def _horizon(period: int) -> HorizonTrend | None:
        return horizon_trend(
            closes,
            period,
            deadband_percent=deadband_percent,
            price_deadband_percent=price_deadband_percent,
        )

    return TrendAssessment(
        symbol=series.symbol,
        timeframe=series.timeframe,
        reference_price=round(reference_price, 2),
        short_term=_horizon(short_period),
        medium_term=_horizon(medium_period),
        long_term=_horizon(long_period),
    )


# =============================== Technical-indicator bundle ===============================
#
# The catalogue the ``/indicators`` endpoint serves: any subset can be requested in
# one call, each computed from the same OHLCV bars the chart already fetches (no new
# port, no new data source). Every indicator here is a *pure function of a candle
# series* — same spirit as EMA and support levels above.
#
# Two shapes come out the other side:
#   • an **overlay** (drawn on the candle chart's price axis — SMA/EMA/Bollinger/VWAP),
#   • or a **separate pane** (its own scale — RSI/MACD/ATR/Stochastic/ADX/OBV/…).
#
# Each indicator is one or more named *lines* (e.g. MACD → macd/signal/histogram,
# Bollinger → upper/middle/lower). Every compute function returns values that are
# **tail-aligned**: the result covers the *last* ``len(values)`` bars of the input, so
# a caller aligns them to ``candles[-len(values):]`` — exactly how ``ema_line`` above
# aligns ``compute_ema``. A series too short to define an indicator yields ``[]`` (an
# empty line), never an error — "couldn't compute it yet" is not a failure.


@dataclass(frozen=True)
class IndicatorPoint:
    timestamp: datetime
    value: float


@dataclass(frozen=True)
class IndicatorLine:
    key: str
    points: tuple[IndicatorPoint, ...]

    @property
    def latest(self) -> IndicatorPoint | None:
        return self.points[-1] if self.points else None


@dataclass(frozen=True)
class Indicator:
    name: str
    label: str
    overlay: bool
    lines: tuple[IndicatorLine, ...]


@dataclass(frozen=True)
class IndicatorSet:
    symbol: str
    timeframe: Timeframe
    indicators: tuple[Indicator, ...]


@dataclass(frozen=True)
class IndicatorSpec:
    name: str
    period: int | None = None


# The standard primary lookback for each single-period indicator (the value a
# `name:period` token overrides). MACD/OBV/VWAP don't have one and are handled apart.
_DEFAULT_PERIODS: dict[str, int] = {
    "rsi": 14,
    "atr": 14,
    "mfi": 14,
    "willr": 14,
    "cci": 20,
    "roc": 12,
    "sma": 50,
    "ema": 21,
    "bbands": 20,
    "stoch": 14,
    "adx": 14,
}

# Indicators with no single primary period — a period override is a 400.
_NO_PERIOD: frozenset[str] = frozenset({"macd", "obv", "vwap"})

# Price-axis overlays (drawn on the candles); everything else is a separate pane.
_OVERLAY: frozenset[str] = frozenset({"sma", "ema", "bbands", "vwap"})

# The full set of accepted indicator names.
INDICATOR_NAMES: frozenset[str] = _NO_PERIOD | frozenset(_DEFAULT_PERIODS)

# MACD's fixed sub-parameters (only the standard triple is offered — no per-line tuning).
_MACD_FAST, _MACD_SLOW, _MACD_SIGNAL = 12, 26, 9
# Bollinger band width, in standard deviations.
_BBANDS_STDDEV = 2.0
# Stochastic smoothing (%K SMA) and signal (%D SMA) lengths.
_STOCH_SMOOTH, _STOCH_SIGNAL = 3, 3


def _resolve_period(name: str, override: int | None) -> int:
    if name not in INDICATOR_NAMES:
        raise ValueError(f"Unknown indicator '{name}'.")
    if name in _NO_PERIOD:
        if override is not None:
            raise ValueError(f"Indicator '{name}' does not take a period.")
        return 0
    period = override if override is not None else _DEFAULT_PERIODS[name]
    if period < 2:
        raise ValueError("Indicator period must be at least 2.")
    return period


def indicator_warmup_bars(name: str, period: int | None = None) -> int:
    if name in ("obv", "vwap"):
        return 0
    if name == "macd":
        return _MACD_SLOW + _MACD_SIGNAL
    base = period if period is not None else _DEFAULT_PERIODS[name]
    if name == "adx":
        return 2 * base
    if name == "stoch":
        return base + _STOCH_SMOOTH + _STOCH_SIGNAL
    return base


# --------------------------- pure math (tail-aligned value lists) ---------------------------


def _sma(values: Sequence[float], period: int) -> list[float]:
    if period < 1:
        raise ValueError("SMA period must be at least 1.")
    n = len(values)
    if n < period:
        return []
    running = sum(values[:period])
    out = [running / period]
    for i in range(period, n):
        running += values[i] - values[i - period]
        out.append(running / period)
    return out


def compute_sma(closes: Sequence[float], period: int) -> list[float]:
    return [round(v, 4) for v in _sma(closes, period)]


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0  # all up → 100; dead flat → neutral 50
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def compute_rsi(closes: Sequence[float], period: int = 14) -> list[float]:
    if period < 1:
        raise ValueError("RSI period must be at least 1.")
    n = len(closes)
    if n <= period:
        return []
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain, avg_loss = gains / period, losses / period
    out = [_rsi_value(avg_gain, avg_loss)]
    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out.append(_rsi_value(avg_gain, avg_loss))
    return out


def compute_macd(
    closes: Sequence[float],
    fast: int = _MACD_FAST,
    slow: int = _MACD_SLOW,
    signal: int = _MACD_SIGNAL,
) -> tuple[list[float], list[float], list[float]]:
    if fast < 1 or slow < 1 or signal < 1:
        raise ValueError("MACD periods must be at least 1.")
    if fast >= slow:
        raise ValueError("MACD fast period must be shorter than the slow period.")
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    if not ema_slow:
        return [], [], []
    fast_tail = ema_fast[-len(ema_slow):]  # align both to where the slow EMA exists
    macd_line = [f - s for f, s in zip(fast_tail, ema_slow)]
    signal_line = compute_ema(macd_line, signal)
    if not signal_line:
        return macd_line, [], []
    hist_tail = macd_line[-len(signal_line):]
    histogram = [m - s for m, s in zip(hist_tail, signal_line)]
    return macd_line, signal_line, histogram


def compute_bollinger(
    closes: Sequence[float], period: int = 20, num_std: float = _BBANDS_STDDEV
) -> tuple[list[float], list[float], list[float]]:
    if period < 1:
        raise ValueError("Bollinger period must be at least 1.")
    if num_std < 0:
        raise ValueError("Bollinger num_std must be non-negative.")
    n = len(closes)
    if n < period:
        return [], [], []
    upper: list[float] = []
    middle: list[float] = []
    lower: list[float] = []
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        sd = math.sqrt(variance)
        middle.append(mean)
        upper.append(mean + num_std * sd)
        lower.append(mean - num_std * sd)
    return upper, middle, lower


def _true_ranges(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    return [
        max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        for i in range(1, len(closes))
    ]


def compute_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float]:
    if period < 1:
        raise ValueError("ATR period must be at least 1.")
    if not len(highs) == len(lows) == len(closes):
        raise ValueError("highs, lows and closes must be the same length.")
    n = len(closes)
    if n <= period:
        return []
    trs = _true_ranges(highs, lows, closes)
    atr = sum(trs[:period]) / period
    out = [atr]
    for j in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[j]) / period
        out.append(atr)
    return out


def compute_stochastic(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    k_period: int = 14,
    smooth: int = _STOCH_SMOOTH,
    d_period: int = _STOCH_SIGNAL,
) -> tuple[list[float], list[float]]:
    if k_period < 1 or smooth < 1 or d_period < 1:
        raise ValueError("Stochastic periods must be at least 1.")
    if not len(highs) == len(lows) == len(closes):
        raise ValueError("highs, lows and closes must be the same length.")
    n = len(closes)
    if n < k_period:
        return [], []
    raw: list[float] = []
    for i in range(k_period - 1, n):
        hh = max(highs[i - k_period + 1 : i + 1])
        ll = min(lows[i - k_period + 1 : i + 1])
        raw.append(50.0 if hh == ll else 100.0 * (closes[i] - ll) / (hh - ll))
    k_line = _sma(raw, smooth)
    d_line = _sma(k_line, d_period)
    return k_line, d_line


def _dx(plus_di: float, minus_di: float) -> float:
    total = plus_di + minus_di
    return 100.0 * abs(plus_di - minus_di) / total if total else 0.0


def compute_adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> tuple[list[float], list[float], list[float]]:
    if period < 1:
        raise ValueError("ADX period must be at least 1.")
    if not len(highs) == len(lows) == len(closes):
        raise ValueError("highs, lows and closes must be the same length.")
    n = len(closes)
    if n <= period:
        return [], [], []
    trs = _true_ranges(highs, lows, closes)
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    if len(trs) < period:
        return [], [], []
    sm_tr = sum(trs[:period])
    sm_pdm = sum(plus_dm[:period])
    sm_mdm = sum(minus_dm[:period])

    def _di(sp: float, sm: float, st: float) -> tuple[float, float]:
        return (100.0 * sp / st if st else 0.0, 100.0 * sm / st if st else 0.0)

    p, m = _di(sm_pdm, sm_mdm, sm_tr)
    plus_di, minus_di, dx = [p], [m], [_dx(p, m)]
    for j in range(period, len(trs)):
        sm_tr = sm_tr - sm_tr / period + trs[j]
        sm_pdm = sm_pdm - sm_pdm / period + plus_dm[j]
        sm_mdm = sm_mdm - sm_mdm / period + minus_dm[j]
        p, m = _di(sm_pdm, sm_mdm, sm_tr)
        plus_di.append(p)
        minus_di.append(m)
        dx.append(_dx(p, m))
    if len(dx) < period:
        return [], plus_di, minus_di
    adx_val = sum(dx[:period]) / period
    adx = [adx_val]
    for j in range(period, len(dx)):
        adx_val = (adx_val * (period - 1) + dx[j]) / period
        adx.append(adx_val)
    return adx, plus_di, minus_di


def compute_obv(closes: Sequence[float], volumes: Sequence[int | None]) -> list[float]:
    n = len(closes)
    if n == 0:
        return []
    obv = 0.0
    out = [obv]
    for i in range(1, n):
        vol = volumes[i] or 0
        if closes[i] > closes[i - 1]:
            obv += vol
        elif closes[i] < closes[i - 1]:
            obv -= vol
        out.append(obv)
    return out


def compute_vwap(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[int | None],
) -> list[float]:
    n = len(closes)
    if n == 0:
        return []
    cum_pv = 0.0
    cum_vol = 0.0
    out: list[float] = []
    for i in range(n):
        typical = (highs[i] + lows[i] + closes[i]) / 3.0
        vol = volumes[i] or 0
        cum_pv += typical * vol
        cum_vol += vol
        out.append(cum_pv / cum_vol if cum_vol else typical)
    return out


def compute_williams_r(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float]:
    if period < 1:
        raise ValueError("Williams %R period must be at least 1.")
    if not len(highs) == len(lows) == len(closes):
        raise ValueError("highs, lows and closes must be the same length.")
    n = len(closes)
    if n < period:
        return []
    out: list[float] = []
    for i in range(period - 1, n):
        hh = max(highs[i - period + 1 : i + 1])
        ll = min(lows[i - period + 1 : i + 1])
        out.append(-50.0 if hh == ll else -100.0 * (hh - closes[i]) / (hh - ll))
    return out


def compute_cci(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 20,
) -> list[float]:
    if period < 1:
        raise ValueError("CCI period must be at least 1.")
    if not len(highs) == len(lows) == len(closes):
        raise ValueError("highs, lows and closes must be the same length.")
    n = len(closes)
    if n < period:
        return []
    typical = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    out: list[float] = []
    for i in range(period - 1, n):
        window = typical[i - period + 1 : i + 1]
        mean = sum(window) / period
        mad = sum(abs(x - mean) for x in window) / period
        out.append(0.0 if mad == 0 else (typical[i] - mean) / (0.015 * mad))
    return out


def compute_roc(closes: Sequence[float], period: int = 12) -> list[float]:
    if period < 1:
        raise ValueError("ROC period must be at least 1.")
    n = len(closes)
    if n <= period:
        return []
    out: list[float] = []
    for i in range(period, n):
        prior = closes[i - period]
        out.append(100.0 * (closes[i] - prior) / prior if prior else 0.0)
    return out


def compute_mfi(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[int | None],
    period: int = 14,
) -> list[float]:
    if period < 1:
        raise ValueError("MFI period must be at least 1.")
    if not len(highs) == len(lows) == len(closes) == len(volumes):
        raise ValueError("highs, lows, closes and volumes must be the same length.")
    n = len(closes)
    if n <= period:
        return []
    typical = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    raw_flow = [typical[i] * (volumes[i] or 0) for i in range(n)]
    out: list[float] = []
    for i in range(period, n):
        positive = 0.0
        negative = 0.0
        for j in range(i - period + 1, i + 1):
            if typical[j] > typical[j - 1]:
                positive += raw_flow[j]
            elif typical[j] < typical[j - 1]:
                negative += raw_flow[j]
        if negative == 0:
            out.append(100.0 if positive > 0 else 50.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + positive / negative))
    return out


# --------------------------- builders (candles → Indicator) ---------------------------


def _line(key: str, candles: tuple, values: Sequence[float]) -> IndicatorLine:
    tail = candles[len(candles) - len(values):]
    points = tuple(
        IndicatorPoint(timestamp=candle.timestamp, value=round(value, 4))
        for candle, value in zip(tail, values)
    )
    return IndicatorLine(key=key, points=points)


def build_indicator(series: CandleSeries, spec: IndicatorSpec) -> Indicator:
    name = spec.name
    period = _resolve_period(name, spec.period)
    candles = series.candles
    overlay = name in _OVERLAY
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]

    def done(label: str, lines: list[IndicatorLine]) -> Indicator:
        return Indicator(name=name, label=label, overlay=overlay, lines=tuple(lines))

    if name == "sma":
        return done(f"SMA ({period})", [_line("sma", candles, compute_sma(closes, period))])
    if name == "ema":
        return done(f"EMA ({period})", [_line("ema", candles, compute_ema(closes, period))])
    if name == "rsi":
        return done(f"RSI ({period})", [_line("rsi", candles, compute_rsi(closes, period))])
    if name == "macd":
        macd_line, signal_line, histogram = compute_macd(closes)
        return done(
            f"MACD ({_MACD_FAST}/{_MACD_SLOW}/{_MACD_SIGNAL})",
            [
                _line("macd", candles, macd_line),
                _line("signal", candles, signal_line),
                _line("histogram", candles, histogram),
            ],
        )
    if name == "bbands":
        upper, middle, lower = compute_bollinger(closes, period)
        return done(
            f"Bollinger Bands ({period}, {_BBANDS_STDDEV:g}σ)",
            [
                _line("upper", candles, upper),
                _line("middle", candles, middle),
                _line("lower", candles, lower),
            ],
        )
    if name == "atr":
        return done(f"ATR ({period})", [_line("atr", candles, compute_atr(highs, lows, closes, period))])
    if name == "stoch":
        k_line, d_line = compute_stochastic(highs, lows, closes, period)
        return done(
            f"Stochastic ({period}/{_STOCH_SMOOTH}/{_STOCH_SIGNAL})",
            [_line("k", candles, k_line), _line("d", candles, d_line)],
        )
    if name == "adx":
        adx, plus_di, minus_di = compute_adx(highs, lows, closes, period)
        return done(
            f"ADX ({period})",
            [
                _line("adx", candles, adx),
                _line("plus_di", candles, plus_di),
                _line("minus_di", candles, minus_di),
            ],
        )
    if name == "obv":
        return done("OBV", [_line("obv", candles, compute_obv(closes, volumes))])
    if name == "vwap":
        return done("VWAP", [_line("vwap", candles, compute_vwap(highs, lows, closes, volumes))])
    if name == "willr":
        return done(
            f"Williams %R ({period})",
            [_line("willr", candles, compute_williams_r(highs, lows, closes, period))],
        )
    if name == "cci":
        return done(f"CCI ({period})", [_line("cci", candles, compute_cci(highs, lows, closes, period))])
    if name == "roc":
        return done(f"ROC ({period})", [_line("roc", candles, compute_roc(closes, period))])
    if name == "mfi":
        return done(f"MFI ({period})", [_line("mfi", candles, compute_mfi(highs, lows, closes, volumes, period))])
    # Unreachable: _resolve_period already rejected any unknown name.
    raise ValueError(f"Unknown indicator '{name}'.")


def build_indicators(
    series: CandleSeries, specs: Sequence[IndicatorSpec]
) -> IndicatorSet:
    return IndicatorSet(
        symbol=series.symbol,
        timeframe=series.timeframe,
        indicators=tuple(build_indicator(series, spec) for spec in specs),
    )
