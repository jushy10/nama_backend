"""Enterprise Business Rules: the Stock entity.

Pure domain object — imports nothing from the rest of the app, the web
framework, or Alpaca. It only knows the concept of a "stock" and the
calculations intrinsic to it.
"""

from dataclasses import dataclass
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
    price*.
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


class Recommendation(str, Enum):
    """The headline buy / hold / sell call of an AI stock analysis.

    The string values double as the JSON the model returns and the API serves,
    the same convention as ``Timeframe``.
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


class MarketTone(str, Enum):
    """The risk posture the day's sector rotation implies.

    A day where cyclical/growth sectors (tech, discretionary) lead is ``risk_on``
    (appetite for risk); one where defensives (staples, utilities, health care)
    lead is ``risk_off`` (a flight to safety); no clear rotation is ``mixed``. The
    string values double as the JSON the model returns and the API serves, the
    same convention as ``Recommendation``.
    """

    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    MIXED = "mixed"


@dataclass(frozen=True)
class SectorHighlight:
    """One sector called out in a market analysis, with the model's plain note.

    ``change_percent`` is *not* authored by the model — it's joined back from the
    day's board (matched to the sector the model named) so the number on the card
    stays a real quote, never a figure the model invented. ``note`` is the model's
    one-line, plain-language read on why the sector is leading or lagging.
    """

    sector: str
    symbol: str  # the proxy ETF ticker, carried through from the board
    change_percent: float | None
    note: str


@dataclass(frozen=True)
class SectorAnalysis:
    """An AI-generated read on how the market's sectors are moving today.

    The market-wide sibling of ``InvestmentAnalysis``: produced by a language
    model from the day's ranked sector board (each sector's move + trailing
    returns) and nothing else — never outside data the model happens to recall.
    ``summary`` is the plain-language headline of which corners of the market are
    leading and lagging; ``tone`` is the risk posture that rotation implies;
    ``leaders`` and ``laggards`` name the standout sectors with a short note each
    (their ``change_percent`` joined back from the board, not authored). It is
    informational, not personalized advice — the model fills in the substance and
    the presenter attaches the disclaimer. ``model``/``generated_at`` keep a
    cached read traceable, as with ``InvestmentAnalysis``.
    """

    summary: str
    tone: MarketTone
    leaders: tuple[SectorHighlight, ...]
    laggards: tuple[SectorHighlight, ...]
    model: str
    generated_at: datetime


@dataclass(frozen=True)
class MarketIndexPerformance:
    """One headline US index's move on the day, proxied by a tradable ETF.

    Broad-market indices (the S&P 500, the Nasdaq) aren't directly tradable, so
    each is read through the exchange-traded fund that tracks it — SPY for the
    S&P 500, QQQ for the Nasdaq. The day's move is the proxy's latest price versus
    its previous close (the same rule the ``Stock`` and ``SectorPerformance``
    entities use); ``performance`` carries the trailing-window returns
    (1w/1m/…/1y), best-effort like the others (``None`` when price history is
    unavailable).
    """

    name: str  # the index's plain name, e.g. "S&P 500"
    symbol: str  # the proxy ETF ticker, e.g. "SPY"
    price: float  # latest trade price of the proxy ETF
    previous_close: float | None
    as_of: datetime | None
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


class MarketPeriod(str, Enum):
    """A trailing timeframe the market summary reads over — the past week, month,
    or year.

    The string values double as the JSON the model returns and the API serves,
    the same convention as ``MarketTone``.
    """

    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


@dataclass(frozen=True)
class MarketIndexReturn:
    """One index's return over a single timeframe, carried on a period highlight.

    ``change_percent`` is joined from the day's board (a real quote), never
    authored by the model — the same discipline ``SectorHighlight`` follows.
    """

    name: str
    symbol: str  # the proxy ETF ticker, carried through from the board
    change_percent: float | None


@dataclass(frozen=True)
class MarketPeriodHighlight:
    """One timeframe (past week/month/year) in the market summary.

    ``note`` is the model's one-line, plain-language read of how that stretch
    went; ``indexes`` carries each index's real return over the window, joined
    from the board (real quotes) rather than authored by the model.
    """

    period: MarketPeriod
    note: str
    indexes: tuple[MarketIndexReturn, ...]


@dataclass(frozen=True)
class MarketSummary:
    """An AI-generated overview of how the US market has moved lately.

    The market-wide sibling of ``SectorAnalysis``: produced by a language model
    from the day's index board (the S&P 500 and the Nasdaq, each with its
    trailing-window returns) and nothing else — never outside data the model
    happens to recall. ``summary`` is the plain-language headline; ``tone`` is the
    risk posture the recent moves imply; ``periods`` breaks the read down by
    timeframe (the past year, month and week), each with a short note and the
    indexes' real returns (joined from the board, not authored). It is
    informational, not personalized advice — the model fills in the substance and
    the presenter attaches the disclaimer. ``model``/``generated_at`` keep a read
    traceable, as with ``SectorAnalysis``.
    """

    summary: str
    tone: MarketTone
    periods: tuple[MarketPeriodHighlight, ...]
    model: str
    generated_at: datetime


class EarningsTrend(str, Enum):
    """Where a company's earnings story is heading.

    ``accelerating`` when profit/sales growth is picking up (or its beats are
    getting bigger), ``slowing`` when growth is fading or it's starting to miss,
    ``steady`` when it's holding a consistent pace. The string values double as
    the JSON the model returns and the API serves, the same convention as
    ``Recommendation`` and ``MarketTone``.
    """

    ACCELERATING = "accelerating"
    STEADY = "steady"
    SLOWING = "slowing"


@dataclass(frozen=True)
class EarningsAnalysis:
    """An AI-generated, plain-language read of a company's earnings story.

    The earnings-focused sibling of ``InvestmentAnalysis``: produced by a language
    model from the earnings figures the slice already gathers — the recent
    quarterly and annual timelines (beats/misses, EPS and revenue, and the forward
    consensus) — never from outside data the model happens to recall. ``summary``
    is the plain-language headline of how earnings have gone and where they look
    headed; ``trend`` is the direction; ``highlights`` are a few short takeaways.
    It is informational, not personalized advice — the model fills in the substance
    and the presenter attaches the disclaimer. ``model``/``generated_at`` keep a
    read traceable, as with ``InvestmentAnalysis``.
    """

    symbol: str
    summary: str
    trend: EarningsTrend
    highlights: tuple[str, ...]
    model: str
    generated_at: datetime
