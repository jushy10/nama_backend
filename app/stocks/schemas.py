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


class MarketIndexReturnResponse(BaseModel):
    """One headline index's return over a single timeframe.

    `symbol` is the proxy ETF the index is read through (SPY for the S&P 500, QQQ
    for the Nasdaq); `change_percent` is that proxy's real percent move over the
    period (joined from the board, not authored by the model)."""

    name: str
    symbol: str
    change_percent: float | None = None


class MarketPeriodResponse(BaseModel):
    """One timeframe in the market summary — the past week, month, or year.

    `period` is "week"/"month"/"year"; `indexes` carries each index's real return
    over the window; `note` is the AI's one-line, plain-language read of the
    stretch."""

    period: str  # "week" | "month" | "year"
    indexes: list[MarketIndexReturnResponse]
    note: str


class MarketSummaryResponse(BaseModel):
    """An AI-generated overview of how the US market has moved lately.

    `summary` is the plain-language headline; `tone` is the risk posture the
    recent moves imply ("risk_on"/"risk_off"/"mixed"); `periods` breaks the read
    down by timeframe (the past year, month and week), each with the indexes' real
    returns and a one-line note. `disclaimer` is a fixed reminder that this is
    informational, not financial advice — authored by the service, not the model.
    `model` and `generated_at` record what produced the summary and when. Reasoned
    only over the day's index board; descriptive, not advice."""

    summary: str
    tone: str  # "risk_on" | "risk_off" | "mixed"
    periods: list[MarketPeriodResponse]
    disclaimer: str
    model: str  # the model that produced the summary
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


class EarningsAnalysisResponse(BaseModel):
    """An AI-generated, plain-language read of a stock's earnings story.

    ``summary`` is the plain-language headline of how earnings have gone and where
    they look headed; ``trend`` is the direction ("accelerating"/"steady"/
    "slowing"); ``highlights`` are a few short takeaways. ``disclaimer`` is a fixed
    reminder that this is informational, not financial advice — authored by the
    service, not the model. ``model`` and ``generated_at`` record what produced the
    analysis and when. Reasoned only over the recent earnings timelines;
    descriptive, not advice."""

    symbol: str
    summary: str
    trend: str  # "accelerating" | "steady" | "slowing"
    highlights: list[str]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class RatingsAnalysisResponse(BaseModel):
    """An AI-generated, plain-language read of a stock's analyst coverage.

    ``verdict`` is the overall read ("bullish"/"mixed"/"cautious") and ``confidence`` how
    firmly it's held ("low"/"medium"/"high"); ``summary`` is the plain-language headline and
    ``findings`` a few short, concrete takeaways. ``disclaimer`` is a fixed reminder that this
    is informational, not financial advice — authored by the service, not the model. ``model``
    and ``generated_at`` record what produced the analysis and when. Reasoned only over the
    analyst coverage the card exposes; descriptive, not advice."""

    symbol: str
    verdict: str  # "bullish" | "mixed" | "cautious"
    confidence: str  # "low" | "medium" | "high"
    summary: str
    findings: list[str]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class FundamentalsAnalysisResponse(BaseModel):
    """An AI-generated, plain-language read of a stock's fundamentals.

    ``verdict`` is the overall read ("strong"/"mixed"/"weak") of the company's fundamentals —
    profitability, growth, balance-sheet health, and whether the shares look reasonably priced
    against all that — and ``confidence`` how firmly it's held ("low"/"medium"/"high");
    ``summary`` is the plain-language headline and ``findings`` a few short, concrete takeaways.
    ``disclaimer`` is a fixed reminder that this is informational, not financial advice — authored
    by the service, not the model. ``model`` and ``generated_at`` record what produced the
    analysis and when. Reasoned only over the fundamentals the ticker card exposes plus the
    industry-P/E peer benchmark; descriptive, not advice."""

    symbol: str
    verdict: str  # "strong" | "mixed" | "weak"
    confidence: str  # "low" | "medium" | "high"
    summary: str
    findings: list[str]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime
