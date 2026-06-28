"""HTTP response model for the stocks endpoint.

Pydantic is a web/serialization detail, so this DTO lives at the edge —
deliberately separate from the Stock entity so the core stays
framework-agnostic.
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


class KeyMetricsResponse(BaseModel):
    """Trailing valuation, financial-health and market indicators.

    The valuation ratios and risk/range figures for the price snapshot. The
    earnings-flavored metrics (EPS, growth, margins) live on the earnings
    endpoint instead — see ``EarningsMetricsResponse``. All trailing (no forward
    estimates); the ratios are plain multiples. Any field a vendor doesn't cover
    is ``null``.
    """

    pe: float | None = None  # price / trailing EPS
    peg: float | None = None  # trailing P/E / trailing EPS growth (not forward)
    pb: float | None = None  # price / book value
    ps: float | None = None  # price / sales
    current_ratio: float | None = None
    debt_to_equity: float | None = None
    beta: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None


class StockResponse(BaseModel):
    symbol: str
    name: str | None = None
    exchange: str | None = None
    description: str | None = None  # what the company does (best-effort, may be null)
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


class QuoteResponse(BaseModel):
    """A minimal live quote for high-frequency polling.

    The slim counterpart to ``StockResponse``: only the fields a ticking price
    widget redraws. ``change``/``change_percent`` follow the same rules as the
    full stock endpoint, so the two never disagree on the day's move."""

    symbol: str
    price: float
    change: float | None = None
    change_percent: float | None = None
    previous_close: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    as_of: datetime | None = None


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


class EarningsSurpriseResponse(BaseModel):
    """One quarter's reported EPS versus the consensus estimate going in.

    ``beat`` is the met-or-beat flag (``actual >= estimate``); ``null`` when
    either side is missing. ``surprise`` is the EPS gap and ``surprise_percent``
    that gap as a percent of the estimate."""

    period: date | None = None  # fiscal period end date
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    actual: float | None = None  # reported EPS
    estimate: float | None = None  # consensus EPS estimate
    surprise: float | None = None  # actual - estimate (EPS)
    surprise_percent: float | None = None  # percent of estimate
    beat: bool | None = None  # met or beat the estimate
    revenue_actual: float | None = None  # reported revenue for the quarter (raw)


class EarningsMetricsResponse(BaseModel):
    """Trailing earnings / profitability snapshot served with the beat history.

    The income-statement-flavored metrics — trailing EPS, EPS/revenue growth and
    the margin stack. All percentages except ``eps``; any field a vendor doesn't
    cover is ``null``. (Valuation and market metrics live on the stock endpoint
    — see ``KeyMetricsResponse``.)
    """

    eps: float | None = None  # trailing earnings per share
    eps_growth_yoy: float | None = None  # percent
    revenue_growth_yoy: float | None = None  # percent
    gross_margin: float | None = None  # percent
    operating_margin: float | None = None  # percent
    net_margin: float | None = None  # percent


class NextEarningsResponse(BaseModel):
    """The next scheduled earnings report and the consensus going into it.

    The forward complement to the past-only beat history: the expected report
    date and where analysts expect EPS/revenue to land. ``session`` is when in
    the trading day it's expected — "bmo" (before open), "amc" (after close),
    "dmh" (during hours), or ``null``. Estimates are ``null`` when no consensus
    is published yet."""

    report_date: date | None = None  # expected announcement date
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    eps_estimate: float | None = None  # consensus EPS going in
    revenue_estimate: float | None = None  # consensus revenue going in (raw)
    session: str | None = None  # "bmo" | "amc" | "dmh" | null


class EarningsHistoryResponse(BaseModel):
    """Recent quarterly earnings surprises (newest first) plus a beat summary.

    ``beat_rate`` is the percent of *scored* quarters (those with both an actual
    and an estimate) that met or beat — the "beats consistently?" read.
    ``count`` is how many quarters are returned. ``metrics`` is an optional
    trailing earnings snapshot and ``next_report`` the next scheduled report's
    consensus (both best-effort; ``null`` when unavailable)."""

    symbol: str
    count: int
    beats: int  # quarters that met or beat
    scored: int  # quarters with enough data to judge a beat
    beat_rate: float | None = None  # percent of scored quarters that beat
    quarters: list[EarningsSurpriseResponse]
    metrics: EarningsMetricsResponse | None = None
    next_report: NextEarningsResponse | None = None


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


class ScreenedStockResponse(BaseModel):
    """One screener row: a stock's day move plus its universe metadata.

    ``change``/``change_percent`` follow the same rule as every other price
    view, so a name's move here matches its ``/stocks/{symbol}`` move."""

    symbol: str
    name: str | None = None
    sector: str | None = None
    price: float
    change: float | None = None
    change_percent: float | None = None
    previous_close: float | None = None
    as_of: datetime | None = None


class MoversResponse(BaseModel):
    """The day's biggest gainers and losers across a filtered universe.

    ``index``/``sector`` echo the applied filter (``null`` = not filtered).
    ``universe_count`` is how many constituents matched the filter;
    ``quoted_count`` how many of those had a usable live quote and so could be
    ranked. ``gainers`` lead with the largest gain and ``losers`` with the
    largest loss, each capped at ``limit``; a symbol never appears in both."""

    index: str | None = None
    sector: str | None = None
    limit: int
    universe_count: int
    quoted_count: int
    as_of: datetime | None = None
    gainers: list[ScreenedStockResponse]
    losers: list[ScreenedStockResponse]
