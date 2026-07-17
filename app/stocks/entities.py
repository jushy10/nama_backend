"""Enterprise Business Rules: the Stock entity.

Pure domain object — imports nothing from the rest of the app, the web
framework, or Alpaca. It only knows the concept of a "stock" and the
calculations intrinsic to it.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo

# Canadian venue suffixes, in Yahoo's convention — the marker that a ticker is a Canadian
# listing (TSX ``.TO`` / TSX Venture ``.V`` / Cboe Canada ``.NE`` / CSE ``.CN``), the form the
# universe screen stores. Two domain rules ride on them: routing a per-symbol price read
# (US → Alpaca / CA → Yahoo), and deriving a listing's *base* (US-equivalent) ticker so an
# interlisted Canadian listing can be matched to its US sibling and deduped.
CANADIAN_SUFFIXES = (".TO", ".V", ".NE", ".CN")

# Cboe Canada (NEO), Yahoo suffix ``.NE`` — the venue Canadian Depositary Receipts list on. A CDR
# wraps a US / foreign company (``INTC.NE`` → Intel, ``ZAAP.NE`` → Apple), not a Canadian one, so
# the universe deliberately keeps ``.NE`` out of the Canadian screen entirely (genuine Canadian
# companies list on TSX ``.TO`` / TSXV ``.V``).
CBOE_CANADA_SUFFIX = ".NE"


def is_canadian(symbol: str) -> bool:
    """Whether ``symbol`` is a Canadian listing (by Yahoo suffix). Case-insensitive; a blank or
    non-string symbol is not Canadian."""
    if not isinstance(symbol, str):
        return False
    upper = symbol.upper()
    return any(upper.endswith(suffix) for suffix in CANADIAN_SUFFIXES)


def is_cboe_canada(symbol: str) -> bool:
    """Whether ``symbol`` is a Cboe Canada (NEO, ``.NE``) listing — the CDR venue. The universe
    excludes these from the Canadian screen (a CDR is a wrapper of a US / foreign company, not a
    Canadian one). Case-insensitive; a blank or non-string symbol is not Cboe Canada."""
    return isinstance(symbol, str) and symbol.upper().endswith(CBOE_CANADA_SUFFIX)


def base_ticker(symbol: str) -> str:
    """The listing's *base* ticker: a Canadian symbol with its venue suffix stripped
    (``SHOP.TO`` → ``SHOP``, ``AAPL.NE`` → ``AAPL``), any other symbol returned unchanged.

    This is the key an **interlisted** Canadian listing shares with its US sibling — a CDR
    (``AAPL.NE`` wraps ``AAPL``) or a dual-listed Canadian company whose ticker matches its US
    line (``SHOP.TO`` ↔ ``SHOP``) — so the universe dedup can keep the US listing and hide the
    Canadian duplicate. It does *not* catch a dual-listing whose Canadian ticker differs from
    its US one (``CNR.TO`` ↔ ``CNI``); that needs a name match, deliberately out of scope here.
    """
    if not isinstance(symbol, str):
        return symbol
    upper = symbol.upper()
    for suffix in CANADIAN_SUFFIXES:
        if upper.endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


def normalize_symbol(symbol: str, *, kind: str = "stock", article: str = "A") -> str:
    """Trim/upper-case a ticker and reject obvious junk — once, at the edge of a use case,
    so every layer below sees a clean symbol. This is the single guard every per-symbol read
    shares (each slice's ``_normalize_symbol`` delegates here).

    A Canadian venue suffix (``.TO`` / ``.V`` / ``.NE`` / ``.CN``) is **preserved**, so the
    per-symbol price router can still dispatch on it (``is_canadian``) — only the *base* ticker
    is validated (see :func:`_is_valid_base`). A US symbol comes back unchanged (``AAPL`` →
    ``AAPL``); a Canadian one keeps its suffix (``SHOP.TO`` → ``SHOP.TO``); a class/series line
    keeps its dash (``BRK-B`` → ``BRK-B``, ``CAR-UN.TO`` → ``CAR-UN.TO``); junk (a non-letter
    base, an over-long base, or a trailing string that isn't a known venue suffix) is a
    ``ValueError``. ``kind`` / ``article`` shape the error text so a slice keeps its own wording
    ("stock" vs "ETF")."""
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError(f"{article} {kind} symbol is required.")
    base = base_ticker(normalized)  # strip a Canadian venue suffix, if present
    if not _is_valid_base(base):
        raise ValueError(f"'{symbol}' is not a valid {kind} symbol.")
    return normalized


def _is_valid_base(base: str) -> bool:
    """Whether ``base`` (a symbol with any Canadian venue suffix already stripped) is a
    plausible ticker: 1-5 letters, plus an optional ``-``-separated class/series suffix of 1-3
    letters.

    The dash is what carries a **class or series** line in the convention the universe stores
    (Yahoo's): a share class (``BRK-B``, ``TECK-A``, ``RCI-B``), a trust/REIT unit
    (``CAR-UN``, ``BEP-UN``), or a preferred series (``WFC-PC``, ``POW-PE``). Those are ordinary
    listings the screen ingests and values, so rejecting them here would 404 the very rows the
    search list serves.

    Note the dot stays invalid (``AA.B``): Alpaca writes a class share ``BRK.B`` but the anchor
    stores Yahoo's ``BRK-B``, and this guard validates the *stored* convention. Translating to a
    vendor's spelling is an adapter's job, not the domain's.
    """
    root, dash, series = base.partition("-")
    if not root.isalpha() or len(root) > 5:
        return False
    if not dash:
        return True
    return series.isalpha() and len(series) <= 3


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
    ev_to_ebitda: float | None = None  # enterprise value / trailing EBITDA (capital-structure-neutral)
    eps: float | None = None  # trailing earnings per share
    fcf_per_share: float | None = None  # trailing free cash flow per share
    ocf_per_share: float | None = None  # trailing operating cash flow per share (pre-capex)
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
    fcf_growth_yoy: float | None = None  # trailing free-cash-flow-per-share growth (percent)
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
    *trailing* one-year change from reported figures (the trailing YoY growth carried
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


# US market-session windows in Eastern Time — the business rule for labelling *which
# session a trade printed in*, so a regular-session close can be told apart from an
# extended-hours (pre/after) print when splitting a quote. Deliberately coarse: the
# standard extended-hours window (04:00 pre, 20:00 after), no half-day/holiday nuance —
# that "is the market open right now" judgement belongs to the client's own live clock
# (the frontend's market.ts), which this only feeds a labelled *price* to complement.
_MARKET_TZ = ZoneInfo("America/New_York")
_PRE_OPEN_MIN = 4 * 60  # 04:00 ET
_REGULAR_OPEN_MIN = 9 * 60 + 30  # 09:30 ET
_REGULAR_CLOSE_MIN = 16 * 60  # 16:00 ET
_AFTER_CLOSE_MIN = 20 * 60  # 20:00 ET


class MarketSession(str, Enum):
    """Which part of the US trading day a timestamp falls in, by Eastern Time.

    A ``str`` enum so the presenter serializes the value directly. Only the two
    *extended* sessions ever reach a client (a regular-session print needs no
    special treatment, and "closed" carries no price of its own) — the label
    exists to say a shown price is a pre-market or after-hours one.
    """

    PRE_MARKET = "pre_market"
    REGULAR = "regular"
    AFTER_HOURS = "after_hours"
    CLOSED = "closed"


def market_session_at(moment: datetime) -> MarketSession:
    """The US market session ``moment`` falls in, by its Eastern-Time wall clock.

    Weekend → ``CLOSED``; otherwise bucketed by ET time-of-day into pre-market /
    regular / after-hours / closed. A naive datetime is read as UTC (the price feed
    stamps trades in UTC). Coarse by design — see the module note above; it exists to
    tell a regular close from an extended-hours print, not to be an exact tradable
    calendar (holidays/half-days are the client clock's job).
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    et = moment.astimezone(_MARKET_TZ)
    if et.weekday() >= 5:  # Saturday / Sunday
        return MarketSession.CLOSED
    minutes = et.hour * 60 + et.minute
    if _PRE_OPEN_MIN <= minutes < _REGULAR_OPEN_MIN:
        return MarketSession.PRE_MARKET
    if _REGULAR_OPEN_MIN <= minutes < _REGULAR_CLOSE_MIN:
        return MarketSession.REGULAR
    if _REGULAR_CLOSE_MIN <= minutes < _AFTER_CLOSE_MIN:
        return MarketSession.AFTER_HOURS
    return MarketSession.CLOSED


@dataclass(frozen=True)
class ExtendedHours:
    """A quote's extended-hours split: the regular-session close beside the most
    recent pre/after-hours print.

    The "proper" after-hours read — a client shows ``regular_close`` (with its
    *day* move, ``Quote.regular_change``) as the primary price and this extended
    ``price`` as a secondary "After hours / Pre-market" line, so the two moves stay
    separate rather than blended into one number. ``change`` / ``change_percent``
    are the *extended* move — the print measured against the regular close, i.e.
    what happened after the bell, not since yesterday.

    Only ever built for a genuine extended print (see ``Quote.extended_hours``), so
    ``session`` is always ``PRE_MARKET`` or ``AFTER_HOURS``.
    """

    session: MarketSession  # PRE_MARKET or AFTER_HOURS — the window the print occurred in
    price: float  # the latest extended-hours trade price
    regular_close: float  # the regular-session (16:00 ET) close, the anchor to show as primary
    as_of: datetime | None  # the extended trade's timestamp

    @property
    def change(self) -> float | None:
        """The extended move in price: the print less the regular close (``None`` if
        no close to measure against)."""
        if not self.regular_close:
            return None
        return round(self.price - self.regular_close, 4)

    @property
    def change_percent(self) -> float | None:
        """The extended move as a percent of the regular close."""
        if not self.regular_close:  # None or 0 -> undefined
            return None
        return round((self.price - self.regular_close) / self.regular_close * 100, 2)


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
    price: float  # latest trade price (an extended-hours print when one is more recent)
    previous_close: float | None
    bid: float | None
    ask: float | None
    as_of: datetime | None
    # The regular-session (16:00 ET) close, when the feed carries it. Distinct from
    # ``price`` in pre/after-hours (where ``price`` is the extended print): it's the anchor
    # the extended move is measured against. ``None`` for feeds that don't split the day
    # (the Canadian Yahoo feed) — those simply never surface an extended-hours block.
    regular_close: float | None = None

    @property
    def change(self) -> float | None:
        """Absolute price change since the previous close.

        Measured off ``price`` (the latest print), so in extended hours this is the
        *blended* move — yesterday's close to the after-hours print. The day-only move
        is ``regular_change``; the after-hours-only move is ``extended_hours.change``.
        """
        if self.previous_close is None:
            return None
        return round(self.price - self.previous_close, 4)

    @property
    def change_percent(self) -> float | None:
        """Percent price change since the previous close (off ``price``; see ``change``)."""
        if not self.previous_close:  # None or 0 -> undefined
            return None
        return round((self.price - self.previous_close) / self.previous_close * 100, 2)

    @property
    def regular_change(self) -> float | None:
        """The regular session's move: the regular close less the previous close.

        The "day" change to show beside the regular price during extended hours — as
        opposed to ``change``, which is measured off the latest (possibly extended)
        print. ``None`` until both closes are known."""
        if self.regular_close is None or self.previous_close is None:
            return None
        return round(self.regular_close - self.previous_close, 4)

    @property
    def regular_change_percent(self) -> float | None:
        """The regular session's move as a percent of the previous close."""
        if self.regular_close is None or not self.previous_close:
            return None
        return round((self.regular_close - self.previous_close) / self.previous_close * 100, 2)

    @property
    def extended_hours(self) -> "ExtendedHours | None":
        """The extended-hours split, when the latest print is a pre/after-hours one.

        ``None`` during the regular session (``price``/``change`` already tell the
        story) or when the feed carries no regular close to anchor against. Overnight
        and at the weekend the latest print is still the prior session's after-hours
        trade, so this stays populated — the last-known extended price — and the
        client's own clock decides how prominently to surface it.
        """
        if self.regular_close is None or self.as_of is None:
            return None
        session = market_session_at(self.as_of)
        if session not in (MarketSession.PRE_MARKET, MarketSession.AFTER_HOURS):
            return None
        return ExtendedHours(
            session=session,
            price=self.price,
            regular_close=self.regular_close,
            as_of=self.as_of,
        )

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
