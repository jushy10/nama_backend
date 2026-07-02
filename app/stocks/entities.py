"""Enterprise Business Rules: the Stock entity.

Pure domain object — imports nothing from the rest of the app, the web
framework, or Alpaca. It only knows the concept of a "stock" and the
calculations intrinsic to it.
"""

from dataclasses import astuple, dataclass
from datetime import date, datetime
from enum import Enum


class Timeframe(str, Enum):
    """How much time each candle covers — the chart's granularity.

    Vendor-agnostic on purpose: the core only knows these business-level
    granularities; the adapter maps them onto whatever the data vendor calls
    them. The string values double as the API's accepted query values.
    """

    MIN_1 = "1Min"
    MIN_5 = "5Min"
    MIN_15 = "15Min"
    MIN_30 = "30Min"
    HOUR_1 = "1Hour"
    HOUR_4 = "4Hour"
    DAY_1 = "1Day"
    WEEK_1 = "1Week"
    MONTH_1 = "1Month"


@dataclass(frozen=True)
class Logo:
    """A company's logo image plus its MIME type, ready to serve as-is."""

    content: bytes
    media_type: str


@dataclass(frozen=True)
class StockPerformance:
    """Trailing price return over standard windows, expressed as percentages.

    Each field is the percent change of the latest price versus the close at
    the start of that window (``ytd`` is measured from the previous year's
    final close). ``None`` means there isn't enough price history to cover it.
    """

    one_week: float | None
    one_month: float | None
    three_month: float | None
    six_month: float | None
    ytd: float | None
    one_year: float | None


@dataclass(frozen=True)
class KeyMetrics:
    """Trailing valuation, profitability, health and growth indicators.

    Point-in-time ratios that say where a stock trades and how the business is
    doing. Every field is optional: vendors cover tickers unevenly and this is
    best-effort enrichment, so any unknown value is left ``None``.

    All figures are *trailing* (derived from reported history). Forward-looking
    metrics (forward P/E, analyst price targets) need an estimates feed and are
    deliberately out of scope. Margins, ROE and the growth fields are percent;
    the ratios are plain multiples; the per-share figures (EPS, free cash flow
    per share) and the 52-week prices are in the quote currency. The derived
    ``peg`` property combines two of these.
    """

    # Valuation
    pe: float | None = None  # price / trailing EPS
    pb: float | None = None  # price / book value
    ps: float | None = None  # price / sales
    eps: float | None = None  # trailing earnings per share
    fcf_per_share: float | None = None  # trailing free cash flow per share
    # Profitability (percent)
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    roe: float | None = None  # return on equity (percent)
    # Financial health
    current_ratio: float | None = None  # current assets / current liabilities
    debt_to_equity: float | None = None  # total debt / equity
    # Growth (percent, year over year)
    eps_growth_yoy: float | None = None
    revenue_growth_yoy: float | None = None
    # Market / price
    beta: float | None = None  # volatility vs the market (1.0 = moves with it)
    week_52_high: float | None = None
    week_52_low: float | None = None

    @property
    def peg(self) -> float | None:
        """Trailing PEG: P/E divided by trailing EPS growth (percent).

        A rough "is the P/E justified by growth" read — near 1.0 means the
        price roughly matches growth, well above ~2 means it doesn't. Built
        from trailing figures (not forward analyst estimates), so it answers
        "what growth has the company shown", not "what growth is expected".
        ``None`` unless both inputs are present and positive: a non-positive
        P/E (losses) or non-positive growth makes the ratio meaningless.
        """
        if self.pe is None or self.eps_growth_yoy is None:
            return None
        if self.pe <= 0 or self.eps_growth_yoy <= 0:
            return None
        return round(self.pe / self.eps_growth_yoy, 2)


def _forward_one_year_growth(fy1: float | None, fy2: float | None) -> float | None:
    """One-year forward growth (percent): the FY1 → FY2 change — what next year's
    consensus implies versus this year's.

    A plain point-to-point percentage gain/loss, not a compounded multi-year rate.
    ``None`` unless both years are present and positive (growth off a non-positive
    base is meaningless)."""
    if fy1 is None or fy2 is None or fy1 <= 0 or fy2 <= 0:
        return None
    return round((fy2 / fy1 - 1) * 100, 2)


@dataclass(frozen=True)
class AnalystEstimates:
    """Forward sell-side consensus estimates for a stock's next fiscal years.

    The forward-looking complement to the (trailing) ``KeyMetrics``: where those
    say what the business *has* done, these say what analysts *expect* it to do.
    Sourced from the annual-earnings slice's stored forward years (the same
    consensus the earnings timeline serves) — not the price feed or company
    filings, which carry only reported actuals.

    ``fiscal_year`` / ``period_end`` identify **FY1**, the nearest full fiscal year
    still being estimated; the ``*_fy2`` fields carry the year after, backing the
    one-year forward growth (FY1→FY2). EPS figures are per share; revenue is raw
    (e.g. USD). Best-effort enrichment: every field is optional and the whole block
    is ``is_empty`` when no forward year is known for the symbol.

    The valuation calcs that need a live price (``forward_pe``) or market cap
    (``forward_ps``) take it as an argument rather than storing it — the estimate is
    a fact about the company, the multiple a fact about the company *at today's
    price*, the same split that keeps the trailing P/E off the bare ``EarningsHistory``.
    """

    fiscal_year: int | None  # FY1: the nearest forward fiscal year
    period_end: date | None  # FY1 fiscal period-end date
    eps_avg: float | None  # FY1 consensus EPS (mean estimate)
    revenue_avg: float | None  # FY1 consensus revenue (raw, e.g. USD)
    fiscal_year_fy2: int | None = None  # FY2: the fiscal year after FY1
    eps_avg_fy2: float | None = None  # FY2 consensus EPS
    revenue_avg_fy2: float | None = None  # FY2 consensus revenue (raw)

    @property
    def is_empty(self) -> bool:
        """True when neither headline estimate is present — nothing worth attaching."""
        return self.eps_avg is None and self.revenue_avg is None

    def forward_pe(self, price: float | None) -> float | None:
        """Forward P/E: ``price`` divided by the FY1 consensus EPS.

        The forward analogue of ``KeyMetrics.pe`` (which divides by *trailing* EPS):
        "what the price implies about *expected* earnings". ``None`` unless the price
        and a *positive* FY1 EPS are both present — a non-positive estimate (an
        expected loss) makes the multiple meaningless, the same guard the trailing
        ``peg`` uses.
        """
        if price is None or self.eps_avg is None or self.eps_avg <= 0:
            return None
        return round(price / self.eps_avg, 2)

    def forward_ps(self, market_cap: float | None) -> float | None:
        """Forward P/S: ``market_cap`` divided by the FY1 consensus revenue.

        ``None`` unless the market cap and a positive FY1 revenue are both present.
        """
        if market_cap is None or not self.revenue_avg or self.revenue_avg <= 0:
            return None
        return round(market_cap / self.revenue_avg, 2)

    def forward_eps_growth(self) -> float | None:
        """Analyst-expected EPS growth next year (FY1 → FY2), percent."""
        return _forward_one_year_growth(self.eps_avg, self.eps_avg_fy2)

    def forward_revenue_growth(self) -> float | None:
        """Analyst-expected revenue growth next year (FY1 → FY2), percent."""
        return _forward_one_year_growth(self.revenue_avg, self.revenue_avg_fy2)


@dataclass(frozen=True)
class GrowthMetrics:
    """Revenue and earnings growth — trailing actuals and forward consensus.

    Two complementary reads on the same two lines (revenue, EPS): ``*_yoy`` is the
    *trailing* one-year change from reported figures (the Finnhub TTM growth carried
    on ``KeyMetrics``); ``forward_*_growth`` is the analyst-*expected* one-year change
    next year — FY1 → FY2 from ``AnalystEstimates``. All percent. Best-effort: any leg
    whose source is absent is ``None``."""

    revenue_yoy: float | None = None  # trailing 1-yr revenue growth (percent)
    eps_yoy: float | None = None  # trailing 1-yr EPS growth (percent)
    forward_revenue_growth: float | None = None  # expected next-yr revenue growth, FY1→FY2 (percent)
    forward_eps_growth: float | None = None  # expected next-yr EPS growth, FY1→FY2 (percent)

    @classmethod
    def build(
        cls,
        metrics: "KeyMetrics | None",
        estimates: "AnalystEstimates | None",
    ) -> "GrowthMetrics | None":
        """Assemble from the trailing ``KeyMetrics`` and forward ``AnalystEstimates``
        already attached to the stock. ``None`` when neither source contributes a
        single growth figure."""
        rev_yoy = metrics.revenue_growth_yoy if metrics else None
        eps_yoy = metrics.eps_growth_yoy if metrics else None
        fwd_rev = estimates.forward_revenue_growth() if estimates else None
        fwd_eps = estimates.forward_eps_growth() if estimates else None
        if all(v is None for v in (rev_yoy, eps_yoy, fwd_rev, fwd_eps)):
            return None
        return cls(
            revenue_yoy=rev_yoy,
            eps_yoy=eps_yoy,
            forward_revenue_growth=fwd_rev,
            forward_eps_growth=fwd_eps,
        )


@dataclass(frozen=True)
class CompanyProfile:
    """A company's clean display name.

    Sourced from a company-profile vendor, not the price feed. Market data APIs
    (e.g. Alpaca) return a ticker's name and exchange, but Alpaca's name is the
    full legal instrument title ("Apple Inc. Common Stock") rather than the tidy
    display name. The profile vendor carries the clean ``name`` ("Apple Inc.") the
    stock view prefers; ``None`` when the vendor doesn't cover the symbol
    (best-effort enrichment). The call yields more (description, sector, website,
    …), left out until something needs it.
    """

    name: str | None = None


@dataclass(frozen=True)
class StockFundamentals:
    """Company fundamentals that live outside the live price snapshot.

    Sourced from a fundamentals vendor rather than the price feed, since market
    data APIs (e.g. Alpaca) don't expose shares outstanding or dividends. The
    same vendor call also yields the richer ``metrics`` block.
    """

    market_cap: float | None
    dividend_per_share: float | None
    dividend_yield: float | None
    metrics: KeyMetrics | None = None


@dataclass(frozen=True)
class EarningsSurprise:
    """One quarter's reported EPS against the consensus estimate going in.

    The gap between ``actual`` and ``estimate`` is the "earnings surprise" —
    whether the company beat, met, or missed expectations that quarter.
    ``surprise_percent`` expresses that gap as a percent of the estimate. Any
    field can be ``None`` when the vendor didn't cover the quarter fully.
    """

    period: date | None  # fiscal period end date
    fiscal_year: int | None
    fiscal_quarter: int | None
    actual: float | None  # reported EPS
    estimate: float | None  # consensus EPS estimate going in
    surprise: float | None  # actual - estimate (EPS)
    surprise_percent: float | None  # surprise as a percent of the estimate
    # Revenue actually reported for the quarter (raw, e.g. USD) — best-effort,
    # overlaid from the quarterly-earnings slice's stored rows. ``None`` when no
    # stored quarter aligns to this one.
    revenue_actual: float | None = None

    @property
    def beat(self) -> bool | None:
        """Whether the quarter met or beat its estimate (``actual >= estimate``).

        Meeting counts as a beat. ``None`` when either side is missing, so an
        unknowable quarter stays distinct from a genuine miss.
        """
        if self.actual is None or self.estimate is None:
            return None
        return self.actual >= self.estimate


@dataclass(frozen=True)
class EarningsMetrics:
    """Trailing earnings / profitability metrics for one stock.

    The income-statement-flavored slice of the broader fundamentals: trailing
    EPS, the year-over-year growth in EPS and revenue, and the margin stack. A
    projection of ``KeyMetrics`` that rides alongside the quarterly beat history,
    so the earnings view answers "how profitable, and growing how fast?" without a
    second call. The valuation/market indicators (P/E, P/B, beta, …) stay with
    the price snapshot. Every field is optional and all are percentages except
    ``eps``.
    """

    eps: float | None = None  # trailing earnings per share
    eps_growth_yoy: float | None = None  # percent, year over year
    revenue_growth_yoy: float | None = None  # percent, year over year
    gross_margin: float | None = None  # percent
    operating_margin: float | None = None  # percent
    net_margin: float | None = None  # percent

    @classmethod
    def from_key_metrics(cls, metrics: "KeyMetrics | None") -> "EarningsMetrics | None":
        """Project the earnings-flavored fields out of a ``KeyMetrics``.

        ``None`` when there's nothing to carry — no metrics at all, or none of
        the earnings fields are populated — so an uncovered symbol yields no
        metrics block rather than an all-null one (mirroring how the provider
        builds ``KeyMetrics`` itself).
        """
        if metrics is None:
            return None
        projected = cls(
            eps=metrics.eps,
            eps_growth_yoy=metrics.eps_growth_yoy,
            revenue_growth_yoy=metrics.revenue_growth_yoy,
            gross_margin=metrics.gross_margin,
            operating_margin=metrics.operating_margin,
            net_margin=metrics.net_margin,
        )
        if all(value is None for value in astuple(projected)):
            return None
        return projected


@dataclass(frozen=True)
class NextEarnings:
    """The next scheduled earnings report and the consensus going into it.

    The forward complement to the (past-only) beat history: when the company is
    expected to report next, and where analysts expect EPS/revenue to land.
    ``session`` is when in the trading day it's expected — "bmo" (before market
    open), "amc" (after market close), "dmh" (during market hours), or ``None``.
    The estimates are ``None`` when no consensus has been published yet, and the
    whole block is best-effort: absent when nothing is scheduled.
    """

    report_date: date | None  # expected announcement date
    fiscal_year: int | None
    fiscal_quarter: int | None
    eps_estimate: float | None  # consensus EPS going in
    revenue_estimate: float | None  # consensus revenue going in (raw)
    session: str | None  # "bmo" | "amc" | "dmh" | None


@dataclass(frozen=True)
class EarningsHistory:
    """A run of recent quarterly earnings surprises for one symbol.

    Ordered newest quarter first — the order a "last N quarters" view reads in.
    The summary properties answer the checklist's "beats consistently?" question:
    of the quarters with both an actual and an estimate, how many met or beat.
    ``metrics`` is an optional trailing earnings snapshot, ``valuation`` the
    point-in-time valuation/health/market ratios (P/E, PEG, P/B, beta, the
    52-week range — the same ``KeyMetrics`` the stock snapshot carries), and
    ``next_report`` the next scheduled report's consensus — all best-effort
    enrichment riding along with the per-quarter history.
    """

    symbol: str
    quarters: tuple[EarningsSurprise, ...]
    metrics: EarningsMetrics | None = None
    valuation: KeyMetrics | None = None
    next_report: NextEarnings | None = None

    @property
    def scored(self) -> int:
        """Quarters with enough data to judge a beat (actual and estimate)."""
        return sum(1 for q in self.quarters if q.beat is not None)

    @property
    def beats(self) -> int:
        """Count of quarters that met or beat their estimate."""
        return sum(1 for q in self.quarters if q.beat)

    @property
    def beat_rate(self) -> float | None:
        """Percent of scoreable quarters that met or beat; ``None`` if none are."""
        if self.scored == 0:
            return None
        return round(self.beats / self.scored * 100, 1)


@dataclass(frozen=True)
class RecommendationTrend:
    """Analysts' buy/hold/sell split for one monthly snapshot.

    The five buckets are how many sell-side analysts held each stance that
    period (``strong_buy`` … ``strong_sell``). The derived ``score`` collapses
    them to a single consensus mean on the classic 1 (Strong Buy) … 5 (Strong
    Sell) scale — lower is more bullish — and ``consensus`` maps that mean to a
    five-step label, the same vocabulary the RSI verdict uses so the two reads
    line up.
    """

    period: date  # first day of the month this snapshot covers
    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int

    @property
    def total(self) -> int:
        """How many analysts contributed a rating this period."""
        return self.strong_buy + self.buy + self.hold + self.sell + self.strong_sell

    @property
    def score(self) -> float | None:
        """Consensus mean on the 1 (Strong Buy) … 5 (Strong Sell) scale.

        A single-number read of the split, each bucket weighted by its stance.
        ``None`` when no analyst covers the period — an empty snapshot has no
        consensus to take.
        """
        if self.total == 0:
            return None
        weighted = (
            self.strong_buy * 1
            + self.buy * 2
            + self.hold * 3
            + self.sell * 4
            + self.strong_sell * 5
        )
        return round(weighted / self.total, 2)

    @property
    def consensus(self) -> str | None:
        """The mean mapped to a five-step label (``Strong Buy`` … ``Strong Sell``).

        Half-point bands around each integer: ``<= 1.5`` Strong Buy, ``<= 2.5``
        Buy, ``<= 3.5`` Hold, ``<= 4.5`` Sell, else Strong Sell. ``None`` when
        there's no score (no coverage).
        """
        score = self.score
        if score is None:
            return None
        if score <= 1.5:
            return "Strong Buy"
        if score <= 2.5:
            return "Buy"
        if score <= 3.5:
            return "Hold"
        if score <= 4.5:
            return "Sell"
        return "Strong Sell"


@dataclass(frozen=True)
class AnalystRecommendations:
    """A run of analyst recommendation snapshots for one symbol, newest first.

    Each ``RecommendationTrend`` is a month's buy/hold/sell split, ordered newest
    first like the earnings history. ``latest`` is the current consensus and
    ``direction`` reads how it shifted from the prior month — the forward-looking
    part, since an upgrade trend tends to lead price. Best-effort: a symbol no
    analyst covers yields an empty ``trends`` tuple, not an error.
    """

    symbol: str
    trends: tuple[RecommendationTrend, ...] = ()

    @property
    def latest(self) -> RecommendationTrend | None:
        """The most recent snapshot, or ``None`` when there's no coverage."""
        return self.trends[0] if self.trends else None

    @property
    def direction(self) -> str | None:
        """How the consensus moved from the prior snapshot to the latest.

        ``"upgraded"`` when the latest mean is more bullish (lower) than the one
        before it, ``"downgraded"`` when less, ``"unchanged"`` when level.
        ``None`` until there are two snapshots with a score to compare — the
        signal is the *shift*, so a lone month doesn't have one yet.
        """
        if len(self.trends) < 2:
            return None
        latest = self.trends[0].score
        prior = self.trends[1].score
        if latest is None or prior is None:
            return None
        if latest < prior:
            return "upgraded"
        if latest > prior:
            return "downgraded"
        return "unchanged"


@dataclass(frozen=True)
class SectorPerformance:
    """One market sector's move on the day, proxied by its sector ETF.

    Sector indices aren't directly tradable, so each sector is represented by
    the SPDR Select Sector ETF that tracks it (e.g. XLK -> Technology). The
    day's move is the proxy's latest price versus its previous close — the same
    rule the Stock entity uses for its own daily change.
    """

    sector: str
    symbol: str  # the proxy ETF ticker
    price: float  # latest trade price of the proxy ETF
    previous_close: float | None
    as_of: datetime | None
    # Trailing-window returns (1w/1m/3m/6m/ytd/1y) of the proxy ETF; best-effort
    # like the Stock entity's, so None when price history is unavailable.
    performance: StockPerformance | None = None

    @property
    def change(self) -> float | None:
        """Absolute price change since the previous close."""
        if self.previous_close is None:
            return None
        return round(self.price - self.previous_close, 4)

    @property
    def change_percent(self) -> float | None:
        """Percent price change since the previous close."""
        if not self.previous_close:  # None or 0 -> undefined
            return None
        return round((self.price - self.previous_close) / self.previous_close * 100, 2)


@dataclass(frozen=True)
class AllTimeHigh:
    """The highest price a stock has reached, and when it got there.

    Derived from the full span of daily price history rather than the live
    snapshot. "All-time" is bounded by how far back that history reaches: a free
    market-data feed may only carry the last several years, so ``since`` records
    the earliest date covered — letting a caller judge whether this high spans
    the stock's whole life or just the vendor's window. ``price`` is the highest
    intraday price seen over that history; ``reached_on`` is the day it occurred.
    """

    price: float  # highest intraday price over the available history
    reached_on: date | None  # the day that high was reached
    since: date | None  # earliest date the underlying history covers (the bound)


@dataclass(frozen=True)
class Stock:
    """A snapshot of a tradable stock at a point in time."""

    symbol: str
    name: str | None
    exchange: str | None
    price: float  # latest trade price
    open: float | None
    high: float | None
    low: float | None
    previous_close: float | None
    volume: int | None
    bid: float | None
    ask: float | None
    as_of: datetime | None
    # Enrichment beyond the raw snapshot; optional so the price-only view of a
    # Stock stays valid when these sources are unavailable (best-effort).
    market_cap: float | None = None
    dividend_per_share: float | None = None
    dividend_yield: float | None = None
    performance: StockPerformance | None = None
    metrics: KeyMetrics | None = None  # trailing valuation/health/market ratios
    analyst_estimates: AnalystEstimates | None = None  # forward consensus (FY1/FY2)
    all_time_high: AllTimeHigh | None = None

    @property
    def change(self) -> float | None:
        """Absolute price change since the previous close."""
        if self.previous_close is None:
            return None
        return round(self.price - self.previous_close, 4)

    @property
    def change_percent(self) -> float | None:
        """Percent price change since the previous close."""
        if not self.previous_close:  # None or 0 -> undefined
            return None
        return round((self.price - self.previous_close) / self.previous_close * 100, 2)

    @property
    def spread(self) -> float | None:
        """Current bid/ask spread, if a quote is available."""
        if self.bid is None or self.ask is None:
            return None
        return round(self.ask - self.bid, 4)

    @property
    def drawdown_from_high(self) -> float | None:
        """Percent the latest price sits below its all-time high (``<= 0``).

        ``0`` means the stock is at a fresh high; ``-18.4`` means 18.4% below it.
        ``None`` when no all-time high is available. Measured against
        ``all_time_high.price``, which the use case has already reconciled with
        the live price (so a stock making a new high reads ``0``, never positive).
        """
        if self.all_time_high is None or not self.all_time_high.price:
            return None
        high = self.all_time_high.price
        return round((self.price - high) / high * 100, 2)

    @property
    def forward_pe(self) -> float | None:
        """Forward P/E from analyst consensus: today's price / FY1 estimated EPS.

        The forward complement to the trailing P/E carried on ``metrics``. ``None``
        when no estimates are attached or the FY1 EPS isn't usable (the calc and its
        guards live on ``AnalystEstimates`` — this just feeds it the live price).
        """
        if self.analyst_estimates is None:
            return None
        return self.analyst_estimates.forward_pe(self.price)

    @property
    def forward_ps(self) -> float | None:
        """Forward P/S from analyst consensus: market cap / FY1 estimated revenue.

        ``None`` when no estimates (or no market cap) are attached.
        """
        if self.analyst_estimates is None:
            return None
        return self.analyst_estimates.forward_ps(self.market_cap)

    @property
    def growth(self) -> GrowthMetrics | None:
        """Revenue/earnings growth — trailing YoY (from ``metrics``) plus forward
        expected CAGR (from ``analyst_estimates``), grouped. Both legs are already
        fetched for the snapshot, so this just combines them; ``None`` when neither
        source is attached."""
        return GrowthMetrics.build(self.metrics, self.analyst_estimates)


@dataclass(frozen=True)
class Quote:
    """A minimal live quote: just enough to redraw a ticking price.

    A deliberately slim cousin of ``Stock`` for high-frequency polling — it
    carries only what a price widget refreshes (last price, the day's change,
    and the bid/ask spread), so serving it costs a single snapshot call with
    none of Stock's company-metadata lookup or best-effort enrichment. The
    change rules are identical to Stock's on purpose: the slim and full views
    must never disagree on the day's move for the same symbol.
    """

    symbol: str
    price: float  # latest trade price
    previous_close: float | None
    bid: float | None
    ask: float | None
    as_of: datetime | None

    @property
    def change(self) -> float | None:
        """Absolute price change since the previous close."""
        if self.previous_close is None:
            return None
        return round(self.price - self.previous_close, 4)

    @property
    def change_percent(self) -> float | None:
        """Percent price change since the previous close."""
        if not self.previous_close:  # None or 0 -> undefined
            return None
        return round((self.price - self.previous_close) / self.previous_close * 100, 2)

    @property
    def spread(self) -> float | None:
        """Current bid/ask spread, if a quote is available."""
        if self.bid is None or self.ask is None:
            return None
        return round(self.ask - self.bid, 4)


@dataclass(frozen=True)
class Candle:
    """One OHLC bar: a stock's price action over a single timeframe slice.

    The building block of a candlestick chart. `is_bullish` is the colour rule
    (green up / red down); it lives here because "did it close above its open"
    is a fact about the candle, not a display choice.
    """

    timestamp: datetime  # the bar's opening time (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: int | None

    @property
    def is_bullish(self) -> bool:
        """True for an up (green) candle — closed at or above its open."""
        return self.close >= self.open


@dataclass(frozen=True)
class CandleSeries:
    """An ordered run of candles for one symbol at one timeframe.

    Candles are chronological (oldest first), the order a chart draws them in
    left to right.
    """

    symbol: str
    timeframe: Timeframe
    candles: tuple[Candle, ...]


class StockIndex(str, Enum):
    """A stock-market index the screener can scope its universe to.

    The string values double as the API's accepted query values, the same
    convention as Timeframe.
    """

    SP500 = "sp500"
    NASDAQ100 = "nasdaq100"


@dataclass(frozen=True)
class Constituent:
    """One member of the screener's universe: a symbol and its memberships.

    Static reference data — which indices a stock belongs to and its GICS
    sector — rather than market data. It tells the screener *what* to rank and
    *how* a caller may narrow the field. ``name``/``sector`` are optional so a
    thinly-covered symbol still screens on price alone.
    """

    symbol: str
    name: str | None
    sector: str | None
    indices: frozenset[str]  # the StockIndex values this symbol belongs to

    def in_index(self, index: StockIndex) -> bool:
        """Whether this constituent is a member of the given index."""
        return index.value in self.indices


@dataclass(frozen=True)
class ScreenedStock:
    """A universe member paired with its live quote — one row of the screener.

    Wraps a ``Quote`` so the day's move follows the exact same rule as every
    other price view, and adds the universe metadata the screener filters and
    labels on. ``symbol`` and ``change_percent`` delegate to the quote so
    ranking and de-duping read naturally.
    """

    name: str | None
    sector: str | None
    quote: Quote

    @property
    def symbol(self) -> str:
        return self.quote.symbol

    @property
    def change_percent(self) -> float | None:
        return self.quote.change_percent


@dataclass(frozen=True)
class MoversBoard:
    """The day's biggest gainers and losers across a (filtered) universe.

    ``gainers`` lead with the largest positive move, ``losers`` with the
    largest negative; a symbol never appears in both. ``index``/``sector`` echo
    the filter that produced the board, and the counts report how wide the
    field was (``universe_count``) versus how many had a usable quote and could
    actually be ranked (``quoted_count``).
    """

    index: StockIndex | None
    sector: str | None
    limit: int
    universe_count: int
    quoted_count: int
    as_of: datetime | None
    gainers: tuple[ScreenedStock, ...]
    losers: tuple[ScreenedStock, ...]


class Recommendation(str, Enum):
    """The headline buy / hold / sell call of an AI stock analysis.

    The string values double as the JSON the model returns and the API serves,
    the same convention as ``Timeframe`` and ``StockIndex``.
    """

    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"


class Confidence(str, Enum):
    """How firmly an analysis holds its recommendation, given the data."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class InvestmentAnalysis:
    """An AI-generated, balanced read on whether a stock looks like a buy.

    Produced by a language model from the figures the rest of the slice already
    gathers — the price snapshot, trailing performance, the valuation/health
    metrics, and the recent earnings beat history — never from outside data the
    model happens to recall. It is informational, not personalized financial
    advice: the model fills in the substance below, and the edge (the presenter)
    is what attaches the disclaimer.

    ``recommendation`` is the headline call and ``confidence`` how firmly it's
    held; ``thesis`` is a few sentences of reasoning, with ``strengths`` (the
    bull case) and ``risks`` (the bear case) as short bullet points. ``model``
    records which model produced it and ``generated_at`` when, so a cached or
    stored analysis stays traceable.
    """

    symbol: str
    recommendation: Recommendation
    confidence: Confidence
    thesis: str
    strengths: tuple[str, ...]
    risks: tuple[str, ...]
    model: str
    generated_at: datetime
