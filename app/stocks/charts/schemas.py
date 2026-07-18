"""HTTP response models for the chart endpoints.

Pydantic is a web/serialization detail, so these DTOs live at the edge —
deliberately separate from the entities so the core stays framework-agnostic.
"""

from datetime import date, datetime

from pydantic import BaseModel


class CandleResponse(BaseModel):
    """One candlestick. `time` is UNIX epoch seconds (UTC) — the format
    charting libraries such as TradingView Lightweight Charts expect — and
    `timestamp` is the same instant in ISO 8601 for human readers."""

    time: int
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    direction: str  # "up" (green) or "down" (red)


class CandleSeriesResponse(BaseModel):
    symbol: str
    timeframe: str
    count: int
    candles: list[CandleResponse]


class EmaPointResponse(BaseModel):
    """One EMA reading. `time` is UNIX epoch seconds (UTC) for charting libs;
    `timestamp` is the same instant in ISO 8601. An EMA rides the price axis, so
    `value` is in the quote currency — an overlay drawn straight on the candles."""

    time: int
    timestamp: datetime
    value: float


class EmaLineResponse(BaseModel):
    """One EMA overlay line at a single period (e.g. the 50-EMA).

    `latest` is the final value (the end that matters for a trend read); a short
    window can leave `points` empty (and `latest` null) when there isn't enough
    history to warm this period up."""

    period: int
    count: int
    latest: float | None = None
    points: list[EmaPointResponse]


class EmaResponse(BaseModel):
    """EMA overlay for a symbol — one line per requested period.

    `lines` are in the order the periods were requested (the classic 20/50/200
    overlay), each an independent line drawn on the candle chart's price axis."""

    symbol: str
    timeframe: str
    lines: list[EmaLineResponse]


class SupportLevelResponse(BaseModel):
    """One horizontal support level — a price zone where the stock has repeatedly
    found buyers (clustered swing lows).

    `strength` is "weak"/"moderate"/"strong" by how many swing lows formed it
    (`touches`); `last_touched` dates the most recent; `distance_percent` is how
    far the level sits below `reference_price` (``<= 0`` — support is under the
    current price)."""

    price: float
    touches: int
    last_touched: date
    strength: str  # "weak" | "moderate" | "strong"
    distance_percent: float


class SupportLevelsResponse(BaseModel):
    """Support levels detected for a symbol, strongest-ranked and returned
    nearest-first (just under the quote).

    `reference_price` is the latest close the levels were measured against — what
    "below the current price" means here. `levels` can be empty when there isn't
    enough history, or no swing low sits below the price, to find any."""

    symbol: str
    timeframe: str
    reference_price: float
    count: int
    levels: list[SupportLevelResponse]


class HorizonTrendResponse(BaseModel):
    """One horizon's trend read, from the slope of its EMA and price's side of it.

    `direction` ("up"/"down"/"sideways") is the EMA's slope over its own timescale.
    `effective_direction` folds that slope together with which side of the line the
    latest close sits on, **price leading**: a line still rising while price has broken
    decisively below it reads "down", because the slope is a trailing average while
    price's side of the line is now. It's the horizon's read to display, and the
    combined `reading` aggregates the three of them, so both track what the chart
    shows; `direction` stays the pure slope for a detail view. `slope_percent` is that
    slope averaged *per bar* (the figure the sideways deadband is applied to);
    `change_percent` is the same move totalled across the `lookback` bars it was
    measured over. `price_vs_ema_percent` is where the latest close sits relative to
    the EMA (positive = above)."""

    period: int
    lookback: int
    direction: str  # "up" | "down" | "sideways" (EMA slope)
    effective_direction: str  # "up" | "down" | "sideways" (slope folded with price side)
    slope_percent: float
    change_percent: float
    price_vs_ema_percent: float
    ema: float


class TrendResponse(BaseModel):
    """A stock's trend at three horizons (short / medium / long), plus their combined
    reading.

    `reading` folds the three horizons into one headline (e.g. "uptrend_weakening" =
    long-term up but mid-term rolling over); it's "unknown" when any horizon lacks the
    history to warm its EMA. `short_term` / `medium_term` / `long_term` are null in
    that same case. `reference_price` is the latest close the read was taken at."""

    symbol: str
    timeframe: str
    reference_price: float
    reading: str
    short_term: HorizonTrendResponse | None = None
    medium_term: HorizonTrendResponse | None = None
    long_term: HorizonTrendResponse | None = None


class IndicatorPointResponse(BaseModel):
    """One indicator reading. `time` is UNIX epoch seconds (UTC) for charting libs;
    `timestamp` is the same instant in ISO 8601."""

    time: int
    timestamp: datetime
    value: float


class IndicatorLineResponse(BaseModel):
    """One named series within an indicator (e.g. MACD's `signal`, Bollinger's
    `upper`). `latest` is the final value; `points` is empty (and `latest` null)
    when there wasn't enough history to compute the line."""

    key: str
    count: int
    latest: float | None = None
    points: list[IndicatorPointResponse]


class IndicatorResponse(BaseModel):
    """One computed indicator: its name, a display `label` carrying the resolved
    parameters (e.g. "RSI (14)"), whether it's a price-axis `overlay` (draw on the
    candles) or a separate pane, and its line(s)."""

    name: str
    label: str
    overlay: bool
    lines: list[IndicatorLineResponse]


class IndicatorsResponse(BaseModel):
    """The technical indicators computed for a symbol, in the order requested.

    Each entry is one indicator (RSI, MACD, Bollinger, …) with one or more lines on
    a shared `time`/`value` shape, so a client renders overlays on the price axis and
    oscillators in their own pane. Computed from the same OHLCV bars the candle
    endpoint serves."""

    symbol: str
    timeframe: str
    count: int
    indicators: list[IndicatorResponse]
