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
