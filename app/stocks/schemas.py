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

    The valuation ratios, returns and risk/range figures for the price snapshot.
    The earnings-flavored metrics (EPS, growth, margins) live on the earnings
    endpoint instead — see ``EarningsMetricsResponse``. All trailing (no forward
    estimates); the ratios are plain multiples, ``roe`` is a percent, and
    ``fcf_per_share`` is in the quote currency. Any field a vendor doesn't cover
    is ``null``.
    """

    pe: float | None = None  # price / trailing EPS
    peg: float | None = None  # trailing P/E / trailing EPS growth (not forward)
    pb: float | None = None  # price / book value
    ps: float | None = None  # price / sales
    fcf_per_share: float | None = None  # trailing free cash flow per share
    roe: float | None = None  # return on equity (percent)
    current_ratio: float | None = None
    debt_to_equity: float | None = None
    beta: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None


class AnalystEstimatesResponse(BaseModel):
    """Forward sell-side consensus estimates for the next fiscal year(s).

    The forward-looking complement to ``KeyMetricsResponse`` (which is all
    trailing): what analysts *expect*, not what the company has reported.
    ``fiscal_year`` is FY1 — the nearest full fiscal year still being estimated —
    and ``eps_avg`` / ``revenue_avg`` its consensus means (``eps_avg_fy2`` is the
    year after, for a next-twelve-months blend). The ``num_analysts_*`` counts
    report consensus breadth. EPS is per share; revenue is raw (e.g. USD). The
    derived multiples ride on the stock response as ``forward_pe`` / ``forward_ps``.
    Any field a vendor doesn't cover is ``null``."""

    fiscal_year: int | None = None  # FY1: the nearest forward fiscal year
    period_end: date | None = None  # FY1 fiscal period-end date
    eps_avg: float | None = None  # FY1 consensus EPS (mean)
    eps_low: float | None = None
    eps_high: float | None = None
    revenue_avg: float | None = None  # FY1 consensus revenue (raw)
    num_analysts_eps: int | None = None
    num_analysts_revenue: int | None = None
    eps_avg_fy2: float | None = None  # FY2 consensus EPS (year after FY1)
    fiscal_year_fy2: int | None = None


class GrowthMetricsResponse(BaseModel):
    """Revenue & earnings growth — trailing actuals plus forward consensus.

    ``*_yoy`` is the *trailing* one-year change from reported figures (Finnhub
    TTM); ``forward_*_growth`` is the analyst-*expected* one-year change next year
    — FY1 → FY2 (FMP estimates). All percent; any leg whose source is unavailable
    is ``null``."""

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
    analyst_estimates: AnalystEstimatesResponse | None = None  # forward consensus
    forward_pe: float | None = None  # price / FY1 estimated EPS (forward, best-effort)
    forward_ps: float | None = None  # market cap / FY1 estimated revenue (forward)
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


class RevenueComponentResponse(BaseModel):
    """One labeled slice of a quarter's revenue and the amount reported for it.

    ``label`` reads as the company names the line in its filing (e.g. "AWS",
    "DRAM", "Online stores"); ``amount`` is the revenue for it (raw, e.g. USD)."""

    label: str
    amount: float


class RevenueBreakdownResponse(BaseModel):
    """A quarter's revenue split by the cuts the filing disclosed.

    ``by_segment`` is the reportable operating segments / business units;
    ``by_product`` the product/service lines. The two are alternate views of the
    same quarter, so each list sums to roughly the quarter's total — they aren't
    additive to each other. A cut the filing doesn't disclose is an empty list."""

    by_segment: list[RevenueComponentResponse] = []
    by_product: list[RevenueComponentResponse] = []


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
    # That revenue split by segment and product/service; null when the filing
    # discloses no breakdown that aligns to the quarter (e.g. a fiscal Q4).
    revenue_breakdown: RevenueBreakdownResponse | None = None


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
    trailing earnings snapshot, ``valuation`` the point-in-time valuation/health/
    market ratios (P/E, PEG, P/B, P/S, beta, the 52-week range — the same block
    the stock endpoint serves), and ``next_report`` the next scheduled report's
    consensus (all best-effort; ``null`` when unavailable)."""

    symbol: str
    count: int
    beats: int  # quarters that met or beat
    scored: int  # quarters with enough data to judge a beat
    beat_rate: float | None = None  # percent of scored quarters that beat
    quarters: list[EarningsSurpriseResponse]
    metrics: EarningsMetricsResponse | None = None
    valuation: KeyMetricsResponse | None = None
    next_report: NextEarningsResponse | None = None


class RecommendationTrendResponse(BaseModel):
    """Analysts' buy/hold/sell split for one monthly snapshot.

    The five buckets are the analyst counts for each stance; ``total`` sums them,
    ``score`` is the consensus mean on the 1 (Strong Buy) … 5 (Strong Sell) scale
    (``null`` with no coverage), and ``consensus`` that mean as a five-step
    label (``Strong Buy`` … ``Strong Sell``)."""

    period: date  # first day of the month the snapshot covers
    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int
    total: int
    score: float | None = None
    consensus: str | None = None


class RecommendationsResponse(BaseModel):
    """Analyst recommendation trends for a symbol, newest snapshot first.

    The forward "what does the street think?" read for the stock page.
    ``latest`` is the current month's split and ``direction`` how the consensus
    shifted from the prior month ("upgraded" / "downgraded" / "unchanged" /
    ``null``) — the predictive part. ``count`` is how many monthly snapshots are
    returned; an empty ``trends`` means no analyst covers the symbol."""

    symbol: str
    count: int
    direction: str | None = None
    latest: RecommendationTrendResponse | None = None
    trends: list[RecommendationTrendResponse]


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
