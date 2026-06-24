"""HTTP response model for the stocks endpoint.

Pydantic is a web/serialization detail, so this DTO lives at the edge —
deliberately separate from the Stock entity so the core stays
framework-agnostic.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class StockPerformanceResponse(BaseModel):
    """Trailing price-return windows (percent), keyed finance-style in JSON.

    Field names are valid Python identifiers; aliases produce the "1w"/"1m"/…
    JSON keys (FastAPI serializes response models by alias).
    """

    model_config = ConfigDict(populate_by_name=True)

    one_week: float | None = Field(default=None, alias="1w")
    one_month: float | None = Field(default=None, alias="1m")
    three_month: float | None = Field(default=None, alias="3m")
    six_month: float | None = Field(default=None, alias="6m")
    ytd: float | None = Field(default=None, alias="ytd")
    one_year: float | None = Field(default=None, alias="1y")


class KeyMetricsResponse(BaseModel):
    """Trailing valuation, profitability, health and growth indicators.

    All trailing (no forward estimates). Margins, ROE and the growth fields are
    percentages; the ratios are plain multiples. Any field a vendor doesn't
    cover is ``null``.
    """

    pe: float | None = None  # price / trailing EPS
    pb: float | None = None  # price / book value
    ps: float | None = None  # price / sales
    eps: float | None = None  # trailing earnings per share
    roe: float | None = None  # return on equity (percent)
    gross_margin: float | None = None  # percent
    operating_margin: float | None = None  # percent
    net_margin: float | None = None  # percent
    current_ratio: float | None = None
    debt_to_equity: float | None = None
    eps_growth_yoy: float | None = None  # percent
    revenue_growth_yoy: float | None = None  # percent
    beta: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None
    payout_ratio: float | None = None  # dividends / earnings (percent)


class StockResponse(BaseModel):
    symbol: str
    name: str | None = None
    exchange: str | None = None
    price: float
    change: float | None = None
    change_percent: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    volume: int | None = None
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    as_of: datetime | None = None
    market_cap: float | None = None  # raw USD
    dividend_per_share: float | None = None  # $ per share, annual
    dividend_yield: float | None = None  # percent
    performance: StockPerformanceResponse | None = None
    metrics: KeyMetricsResponse | None = None


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


class RsiPointResponse(BaseModel):
    """One RSI reading. `time` is UNIX epoch seconds (UTC) for charting libs;
    `timestamp` is the same instant in ISO 8601, and `value` is 0–100."""

    time: int
    timestamp: datetime
    value: float


class RsiResponse(BaseModel):
    """RSI series plus a read of its latest point.

    `signal` is the band the latest value sits in ("overbought" / "oversold" /
    "neutral") — "overbought" being the classic take-profit zone. `overbought`
    and `oversold` carry the threshold lines so a client can draw the bands.
    A short window can leave `points` empty (and `latest`/`signal` null) when
    there isn't enough history to warm the indicator up. Descriptive, not advice.
    """

    symbol: str
    timeframe: str
    period: int
    count: int
    latest: float | None = None
    signal: str | None = None
    overbought: float
    oversold: float
    points: list[RsiPointResponse]
