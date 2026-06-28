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
    deliberately out of scope. Margins and the growth fields are percent; ratios
    are plain multiples; the 52-week prices are in the quote currency. The
    derived ``peg`` property combines two of these.
    """

    # Valuation
    pe: float | None = None  # price / trailing EPS
    pb: float | None = None  # price / book value
    ps: float | None = None  # price / sales
    eps: float | None = None  # trailing earnings per share
    # Profitability (percent)
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
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


@dataclass(frozen=True)
class CompanyProfile:
    """A company's clean display name and a short summary of what it does.

    Sourced from a company-profile vendor, not the price feed. Market data APIs
    (e.g. Alpaca) return a ticker's name and exchange, but Alpaca's name is the
    full legal instrument title ("Apple Inc. Common Stock") rather than the tidy
    display name. The profile vendor carries both a clean ``name`` ("Apple Inc.")
    and the business ``description`` the stock view surfaces; either is ``None``
    when the vendor doesn't cover the symbol (best-effort enrichment). The call
    yields more (sector, website, …), left out until something needs it.
    """

    description: str | None
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
    # Revenue for the quarter (raw, e.g. USD), best-effort from the earnings
    # calendar: ``revenue_estimate`` is the consensus going in, ``revenue_actual``
    # what was reported. ``None`` when the vendor doesn't cover the quarter.
    revenue_estimate: float | None = None
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
class EarningsEstimates:
    """Analyst estimates for one symbol, from an estimates vendor.

    Two slices used to enrich the beat history: ``upcoming`` is the consensus
    for the next several *future* quarters (multiple, not just the next report),
    and ``reported_revenue`` carries each recently-reported quarter's revenue —
    the consensus estimate vs the actual — tagged by its announcement date, so it
    can be matched onto the EPS quarters by period. Best-effort; empty when the
    vendor has nothing.
    """

    upcoming: tuple[NextEarnings, ...] = ()
    # (announcement_date, revenue_estimate, revenue_actual) per reported quarter
    reported_revenue: tuple[tuple[date, float | None, float | None], ...] = ()


@dataclass(frozen=True)
class EarningsHistory:
    """A run of recent quarterly earnings surprises for one symbol.

    Ordered newest quarter first — the order a "last N quarters" view reads in.
    The summary properties answer the checklist's "beats consistently?" question:
    of the quarters with both an actual and an estimate, how many met or beat.
    ``metrics`` is an optional trailing earnings snapshot, ``next_report`` the
    next scheduled report's consensus, and ``upcoming`` the analyst consensus
    for the next several quarters — all best-effort enrichment riding along with
    the per-quarter history.
    """

    symbol: str
    quarters: tuple[EarningsSurprise, ...]
    metrics: EarningsMetrics | None = None
    next_report: NextEarnings | None = None
    upcoming: tuple[NextEarnings, ...] = ()

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
    description: str | None = None  # what the company does, from a profile vendor
    market_cap: float | None = None
    dividend_per_share: float | None = None
    dividend_yield: float | None = None
    performance: StockPerformance | None = None
    metrics: KeyMetrics | None = None

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
