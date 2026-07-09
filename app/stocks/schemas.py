"""HTTP response models for the stocks endpoints.

Pydantic is a web/serialization detail, so these DTOs live at the edge —
deliberately separate from the entities so the core stays framework-agnostic.
"""

from datetime import date, datetime

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


class SectorPerformanceResponse(BaseModel):
    """One market sector's move on the day.

    `symbol` is the proxy ETF the sector is read through (e.g. XLK for
    Technology); `change_percent` is that proxy's percent move on the day."""

    sector: str
    symbol: str
    price: float
    change: float | None = None
    change_percent: float | None = None
    previous_close: float | None = None
    as_of: datetime | None = None
    # Trailing-window returns (percent), keyed 1w/1m/3m/6m/ytd/1y in JSON.
    performance: StockPerformanceResponse | None = None


class SectorBoardResponse(BaseModel):
    """The day's full set of sectors, ranked best performer first."""

    count: int
    sectors: list[SectorPerformanceResponse]


class SectorHighlightResponse(BaseModel):
    """One standout sector in a market analysis, with the AI's plain note.

    `symbol` is the proxy ETF the sector is read through; `change_percent` is that
    proxy's real move on the day (joined from the board, not authored by the model),
    and `note` is the model's one-line read on why it stands out."""

    sector: str
    symbol: str
    change_percent: float | None = None
    note: str


class SectorAnalysisResponse(BaseModel):
    """An AI-generated read of how the market's sectors are moving today.

    `summary` is the plain-language headline of which corners of the market are
    leading and lagging; `tone` is the risk posture the day's rotation implies
    ("risk_on"/"risk_off"/"mixed"); `leaders` and `laggards` are the standout
    sectors with a short note each. `disclaimer` is a fixed reminder that this is
    informational, not financial advice — authored by the service, not the model.
    `model` and `generated_at` record what produced the analysis and when. Reasoned
    only over the day's sector board; descriptive, not advice."""

    summary: str
    tone: str  # "risk_on" | "risk_off" | "mixed"
    leaders: list[SectorHighlightResponse]
    laggards: list[SectorHighlightResponse]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class InvestmentAnalysisResponse(BaseModel):
    """An AI-generated, balanced buy/hold/sell read on a stock.

    ``recommendation`` is the headline call ("buy"/"hold"/"sell") and
    ``confidence`` how firmly it's held ("low"/"medium"/"high"); ``thesis`` is a
    few sentences of reasoning, with ``strengths`` (the bull case) and ``risks``
    (the bear case) as short bullets. ``disclaimer`` is a fixed reminder that this
    is informational, not financial advice — authored by the service, not the
    model. ``model`` and ``generated_at`` record what produced the analysis and
    when. Reasoned only over the figures the other stock endpoints expose;
    descriptive, not advice."""

    symbol: str
    recommendation: str  # "buy" | "hold" | "sell"
    confidence: str  # "low" | "medium" | "high"
    thesis: str
    strengths: list[str]  # bull-case points
    risks: list[str]  # bear-case points
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime
