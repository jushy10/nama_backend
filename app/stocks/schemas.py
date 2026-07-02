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
    """Trailing valuation, financial-health, profitability and market indicators.

    The valuation ratios, returns, margins and risk/range figures for the price
    snapshot; the trailing YoY growth legs ride separately on the stock's
    ``growth`` block. All trailing (no forward estimates); the ratios are plain
    multiples, ``roe`` and the margins are percent, and ``fcf_per_share`` is in
    the quote currency. Any field a vendor doesn't cover is ``null``.
    """

    pe: float | None = None  # price / trailing EPS
    peg: float | None = None  # trailing P/E / trailing EPS growth (not forward)
    pb: float | None = None  # price / book value
    fcf_per_share: float | None = None  # trailing free cash flow per share
    roe: float | None = None  # return on equity (percent)
    # Profitability margins (percent) — on the snapshot so the stock page keeps
    # them once the legacy /earnings endpoint is phased out.
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    current_ratio: float | None = None
    debt_to_equity: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None


class GrowthMetricsResponse(BaseModel):
    """Revenue & earnings growth — trailing actuals plus forward consensus.

    ``*_yoy`` is the *trailing* one-year change from reported figures (Finnhub
    TTM); ``forward_*_growth`` is the analyst-*expected* one-year change next year
    — FY1 → FY2 from the analyst estimates (Yahoo consensus). All percent; any leg
    whose source is unavailable is ``null``."""

    revenue_yoy: float | None = None  # trailing 1-yr revenue growth %
    eps_yoy: float | None = None  # trailing 1-yr EPS growth %
    forward_revenue_growth: float | None = None  # expected next-yr revenue growth (FY1→FY2) %
    forward_eps_growth: float | None = None  # expected next-yr EPS growth (FY1→FY2) %


class AllTimeHighResponse(BaseModel):
    """A stock's all-time high over the available price history.

    ``since`` is the earliest date that history covers — the bound on
    "all-time," since a free feed may not reach back to the stock's listing, so a
    caller can tell a true lifetime high from a within-window one. ``reached_on``
    is when the high occurred. The percent the current price sits below it is
    ``drawdown_from_high`` on the stock."""

    price: float  # highest intraday price over the history
    reached_on: date | None = None  # when the high was reached
    since: date | None = None  # earliest date the history covers (the bound)


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
    metrics: KeyMetricsResponse | None = None  # trailing valuation/health/market
    forward_pe: float | None = None  # price / FY1 estimated EPS (forward, best-effort)
    growth: GrowthMetricsResponse | None = None  # trailing YoY + forward 1-yr growth
    all_time_high: AllTimeHighResponse | None = None
    drawdown_from_high: float | None = None  # percent below the all-time high (<= 0)


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


class GrowthScreenedStockResponse(BaseModel):
    """One growth-screener row: expected next-fiscal-year growth plus its legs.

    ``expected_*_growth`` is the analyst consensus for the upcoming fiscal year
    (``fiscal_year``) versus the latest reported one (``prior_fiscal_year``),
    percent. The estimate/actual legs ride along so a client can show the raw
    figures behind the percentage. Any leg the source didn't cover is ``null``."""

    symbol: str
    name: str | None = None
    sector: str | None = None
    fiscal_year: int | None = None  # the upcoming (FY1) fiscal year
    prior_fiscal_year: int | None = None  # the reported base year
    expected_eps_growth: float | None = None  # percent
    expected_revenue_growth: float | None = None  # percent
    eps_estimate: float | None = None  # FY1 consensus EPS
    eps_actual: float | None = None  # base year's reported EPS
    revenue_estimate: float | None = None  # FY1 consensus revenue (raw)
    revenue_actual: float | None = None  # base year's reported revenue (raw)


class GrowthScreenerResponse(BaseModel):
    """Stocks ranked by expected next-fiscal-year growth across a filtered universe.

    ``index``/``sector``/``sort``/``min_*`` echo the applied filters (``null`` =
    not filtered). ``universe_count`` is how many constituents matched the
    index/sector filter; ``covered_count`` how many of those had stored forward
    consensus to screen on — low coverage means the annual-earnings cache hasn't
    been filled for that universe yet, not that nothing is growing. ``stocks``
    lead with the strongest expected growth on the chosen line, capped at
    ``limit``."""

    index: str | None = None
    sector: str | None = None
    sort: str  # "eps" | "revenue"
    min_revenue_growth: float | None = None  # percent
    min_eps_growth: float | None = None  # percent
    limit: int
    universe_count: int
    covered_count: int
    count: int  # rows returned after thresholds + limit
    stocks: list[GrowthScreenedStockResponse]


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
