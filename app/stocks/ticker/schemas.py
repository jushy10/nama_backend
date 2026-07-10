"""HTTP response DTOs for the ticker endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. That's also why
the renaming is done here: the entity speaks in the domain term ``symbol``; ``ticker``
is this endpoint's JSON vocabulary.

``performance`` reuses the shared ``StockPerformanceResponse`` so those JSON keys stay
one vocabulary across endpoints. ``dividend``, ``performance``, ``metrics`` and
``options_metrics`` are opt-in blocks (the ``include`` query param): ``null`` unless
requested — and for the best-effort ones, ``null`` also when requested but unavailable.
``metrics`` carries ``forward_peg`` — the one figure no other endpoint serves — and is
where future card-only metrics belong; the PEG's legs (``forward_pe``, the forward EPS
growth) stay unserialized, living only on the shared entities that feed the Bedrock
analysis context. ``options_metrics`` is likewise card-only: no other endpoint reads
the options market.
"""

from datetime import date

from pydantic import BaseModel

from app.stocks.schemas import StockPerformanceResponse


class DividendResponse(BaseModel):
    """The stock's dividend, as the fundamentals vendor reports it.

    ``yield_percentage`` is percent; ``per_share`` is the annual payout in the
    quote currency. Both are rounded to 2 decimals at the presenter (the vendor's
    raw figures carry float noise). Either is ``null`` for a non-payer or an
    uncovered field."""

    yield_percentage: float | None = None  # percent, rounded to 2 decimals
    per_share: float | None = None  # $ per share annual, rounded to 2 decimals


class TickerMetricsResponse(BaseModel):
    """The card's valuation and profitability metrics.

    ``pe`` is the trailing multiple on the **analyst-consensus (adjusted) EPS
    basis**: live price over the sum of the 4 newest reported quarters'
    consensus-basis EPS from the quarterly-earnings slice — deliberately not the
    fundamentals vendor's GAAP-ish TTM read, so it sits on the same basis as the
    forward consensus legs (``null`` until 4 quarters are cached, or when the
    trailing year is a loss). Then two PEGs, side by side:
    ``peg`` is the trailing read (the vendor's trailing P/E over
    *already-reported* EPS growth — which a cyclical rebound can inflate and pin
    the ratio near zero), ``forward_peg`` the honest forward cousin (forward P/E
    over the FY1→FY2 growth analysts *expect*); ``forward_peg`` is ``null`` when
    no forward consensus is stored for the symbol yet, a leg is
    non-positive, or expected growth is so near zero that the ratio would explode
    (a boom current year can leave the next single-year leg ~flat). The margins
    are the trailing profitability ladder (percent),
    off the fundamentals call. ``revenue_growth_yoy`` / ``eps_growth_yoy`` are the
    stock's *latest trailing* year-over-year growth (percent) — the newest reported
    fiscal year over the prior one, served straight off the ``stocks`` anchor where
    the annual-earnings slice writes them (EPS on the analyst-consensus basis, to
    match the forward legs); ``null`` until that slice has two reported years cached."""

    pe: float | None = None  # trailing: price / TTM EPS (consensus basis, 4 quarters)
    peg: float | None = None  # trailing P/E / trailing EPS growth
    forward_peg: float | None = None  # forward P/E / expected FY1->FY2 EPS growth
    gross_margin: float | None = None  # percent
    operating_margin: float | None = None  # percent
    net_margin: float | None = None  # percent
    revenue_growth_yoy: float | None = None  # percent, latest trailing YoY (annual slice)
    eps_growth_yoy: float | None = None  # percent, latest trailing YoY, consensus basis


class OptionsMetricsResponse(BaseModel):
    """The options-market read on the stock — four derived figures, not a chain.

    What the market *believes*, for a buyer sizing an entry: how nervous it is
    (``implied_volatility``, at-the-money at the ~1-month expiry, percent), how big
    a swing is priced in by ``expected_move_by`` (``expected_move_percent``, the ATM
    straddle over spot), what a quarter of downside cover costs until
    ``insurance_expires`` (``insurance_cost_percent``, an ATM put over spot), and
    which way today's bets lean (``put_call_ratio`` — above 1 protective, below 1
    optimistic). Every field is independently ``null`` when its contracts are too
    thin to price; all rounded to 2 decimals at the presenter."""

    implied_volatility: float | None = None  # ATM IV at the near expiry, percent
    expected_move_percent: float | None = None  # priced-in swing, percent of spot
    expected_move_by: date | None = None  # the ~1-month expiry sampled
    insurance_cost_percent: float | None = None  # ATM protective put, percent of spot
    insurance_expires: date | None = None  # the ~3-month expiry sampled
    put_call_ratio: float | None = None  # today's put volume / call volume


class TickerCardResponse(BaseModel):
    """A ticker's card: the live quote, name, and the anchor facts, plus opt-in blocks.

    ``ticker`` is the symbol and ``price``/``change``/``change_percent`` the day's
    move (same rules as every other price view); ``name`` is the clean company
    display name, ``exchange`` the listing venue, and ``market_cap`` / ``sector`` /
    ``industry`` the universe-screen facts — all served straight from the ``stocks``
    row (``name``/``exchange`` learned once, the rest written by the universe sync),
    each best-effort and ``null`` until the row carries it (e.g. a stock not yet
    screened has no market cap). ``asset_type`` is the one non-null discriminator —
    ``"etf"`` when the symbol is in the stored ETF universe, else ``"equity"`` — so a
    client can branch the card (and reach for ``GET /stocks/etf/{ticker}`` on a fund).
    ``dividend``, ``performance``, ``metrics`` and ``options_metrics`` appear only when
    asked for via ``?include=`` — ``null`` otherwise, and ``null`` for the best-effort
    ones even when requested if their source is down or keyless."""

    ticker: str
    name: str | None = None  # clean display name ("Micron Technology")
    exchange: str | None = None  # listing venue (e.g. "NASDAQ"); DB-backed
    asset_type: str  # "etf" if in the ETF universe, else "equity" — always present
    price: float
    change: float | None = None  # absolute move vs the previous close
    change_percent: float | None = None  # percent move vs the previous close
    market_cap: float | None = None  # raw USD; from the stocks anchor (universe screen)
    sector: str | None = None  # classification slug; from the stocks anchor
    industry: str | None = None  # classification slug; from the stocks anchor
    dividend: DividendResponse | None = None  # opt-in: ?include=dividend
    performance: StockPerformanceResponse | None = None  # opt-in: ?include=performance
    metrics: TickerMetricsResponse | None = None  # opt-in: ?include=metrics
    options_metrics: OptionsMetricsResponse | None = None  # opt-in: ?include=options_metrics


class PeHistoryPointResponse(BaseModel):
    """One point on the trailing-P/E walk: the P/E at a past earnings release.

    ``date`` is the announcement date, ``price`` the close then, ``ttm_eps`` the
    trailing-twelve-month EPS the market knew (the just-reported quarter plus the three
    before it), and ``pe`` their ratio. All rounded to 2 decimals at the presenter."""

    date: date  # the announcement date the P/E is anchored on
    price: float  # close on/near that date
    ttm_eps: float  # trailing 4 reported quarters' EPS
    pe: float  # price / ttm_eps


class PeHistoryStatsResponse(BaseModel):
    """Where the current trailing P/E sits within the stock's own history — the valuation
    signal derived from the ``points`` series.

    ``current_pe`` is the latest sampled multiple (the most recent earnings release, not a live
    tick — the card's ``metrics.pe`` is that). ``median_pe`` with ``p25_pe``/``p75_pe`` is the
    typical multiple and its interquartile band, and ``min_pe``/``max_pe`` the full envelope —
    the reference line and shaded band a FE draws behind the P/E line. ``current_percentile``
    (0–100) is where the current multiple falls in that distribution and ``signal`` buckets it:
    ``"cheap"`` in the bottom quartile, ``"expensive"`` in the top, ``"fair"`` between.
    ``discount_to_median_percent`` is the gap to the median (negative = below its usual
    multiple). ``sample_size`` is how many releases back the read. A *relative* verdict —
    "cheap for this stock", not "cheap" outright (a re-rated business can read cheap all the way
    down). ``null`` on the parent when the series is too short (< ~2 years) for a percentile to
    mean anything."""

    current_pe: float
    median_pe: float
    p25_pe: float
    p75_pe: float
    min_pe: float
    max_pe: float
    current_percentile: float  # 0–100, share of history at or below the current multiple
    discount_to_median_percent: float  # negative = cheaper than its own median
    signal: str  # "cheap" | "fair" | "expensive"
    sample_size: int


class PeHistoryResponse(BaseModel):
    """A stock's trailing P/E over time — one point per reported quarter, oldest first.

    Derived, not stored: each release's close (Alpaca) over the trailing-twelve-month
    reported EPS (Yahoo) at that date. ``points`` is empty (a 200, not a 404) when the
    EPS history is uncovered or Yahoo blocked the read — the walk is a best-effort card
    extra. The *current* live P/E stays on the card's ``metrics.pe``; this is the
    backward-looking series that pairs with it. ``stats`` distils the series into a
    valuation-vs-history read (percentile + cheap/fair/expensive signal); it's ``null`` for a
    series too short to rank (and absent altogether when ``points`` is empty)."""

    ticker: str
    count: int  # number of points (may be fewer than the reported quarters)
    points: list[PeHistoryPointResponse]  # oldest first
    stats: PeHistoryStatsResponse | None = None  # valuation-vs-history read; null for a thin series


class TickerTypeResponse(BaseModel):
    """A ticker's asset type, from a single ETF-universe membership check.

    The lightweight classifier behind ``GET /stocks/type/{ticker}``: ``ticker``
    echoes the normalized symbol and ``asset_type`` is ``"etf"`` when it's one of
    the screened funds, else ``"equity"``. No quote, no fundamentals — one indexed
    DB read. Always resolves to one of the two, so it never 404s (only a malformed
    symbol is a 400). The same discriminator the ticker card carries, served on
    its own for a caller that only needs the type."""

    ticker: str
    asset_type: str  # "etf" if in the ETF universe, else "equity"