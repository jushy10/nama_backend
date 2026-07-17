"""HTTP response DTOs for the ticker endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. That's also why
the renaming is done here: the entity speaks in the domain term ``symbol``; ``ticker``
is this endpoint's JSON vocabulary.

``performance`` reuses the shared ``StockPerformanceResponse`` so those JSON keys stay
one vocabulary across endpoints. ``dividend``, ``performance``, ``metrics`` and
``options_metrics`` are opt-in blocks (the ``include`` query param): ``null`` unless
requested — and for the best-effort ones, ``null`` also when requested but unavailable.
``metrics`` is where card-only valuation/profitability figures belong, and
``options_metrics`` is likewise card-only: no other endpoint reads the options market.
"""

from datetime import date, datetime

from pydantic import BaseModel

from app.stocks.schemas import StockPerformanceResponse


class ExtendedHoursResponse(BaseModel):
    """The after-hours / pre-market split of the quote, present only when the latest
    trade is an extended-hours print (``null`` during the regular session).

    Lets the FE show the "proper" two-part price a broker shows outside the bell: the
    regular-session close as the primary number with its *day* move, and the extended
    print as a secondary "After hours"/"Pre-market" line with its *own* move. ``session``
    is ``"after_hours"`` or ``"pre_market"``. ``price`` is the latest extended print, and
    ``change``/``change_percent`` are it against ``regular_price`` (the after-bell move).
    ``regular_price`` is the 16:00 ET close and ``regular_change``/``regular_change_percent``
    its move vs the previous close (the day's official move). ``as_of`` is the extended
    trade's timestamp. Overnight/weekends this carries the prior session's last extended
    print; the client's live clock decides how prominently to surface it. US equities only —
    the Canadian (Yahoo) feed doesn't split the day, so it's always ``null`` there."""

    session: str  # "pre_market" | "after_hours"
    price: float  # the latest extended-hours print
    change: float | None = None  # extended move: price vs the regular close
    change_percent: float | None = None
    regular_price: float  # the regular-session (16:00 ET) close — the primary number
    regular_change: float | None = None  # the day's move: regular close vs previous close
    regular_change_percent: float | None = None
    as_of: datetime | None = None  # the extended trade's timestamp


class DividendResponse(BaseModel):
    """The stock's dividend, as the fundamentals vendor reports it.

    ``yield_percentage`` is percent; ``per_share`` is the annual payout in the
    quote currency. Both are rounded to 2 decimals at the presenter (the vendor's
    raw figures carry float noise). Either is ``null`` for a non-payer or an
    uncovered field."""

    yield_percentage: float | None = None  # percent, rounded to 2 decimals
    per_share: float | None = None  # $ per share annual, rounded to 2 decimals


class TickerMetricsResponse(BaseModel):
    """The card's full trailing + forward valuation, profitability, health and growth ladder.

    Everything here is served off the one ``stocks`` anchor read (no live fundamentals
    vendor) except the two price-anchored legs the card prices on its live quote and the
    forward multiples, which ride the annual slice's stored forward consensus.

    **Valuation.** ``pe`` is the trailing multiple on the **analyst-consensus (adjusted) EPS
    basis**: live price over the sum of the 4 newest reported quarters' consensus-basis EPS
    from the quarterly-earnings slice — deliberately not the vendor's GAAP-ish TTM read
    (``null`` until 4 quarters are cached, or when the trailing year is a loss). ``pb`` / ``ps``
    are live price over the anchor's stored per-share book value / sales (``null`` on a
    non-positive input). ``peg`` is ``pe`` over ``eps_growth_yoy`` (both consensus basis;
    ``null`` unless growth is positive). ``eps`` is the trailing TTM EPS (consensus basis) the
    ``pe`` divides by. ``forward_pe`` / ``forward_ps`` are the *forward* multiples — live price
    (or market cap) over the FY1 consensus EPS / revenue the annual slice stores — ``null``
    for an uncovered symbol. ``enterprise_value`` is the whole-business value net of cash at the
    live price (price × shares + total debt − cash, raw USD), and ``ev_ebitda`` is it over
    trailing EBITDA — the capital-structure-neutral multiple that compares across companies with
    different leverage (``null`` on a non-positive EBITDA, the same guard ``pe`` uses on a loss).
    Both ``null`` until the fundamentals sync has landed the EV inputs.

    **Cash flow.** ``price_to_fcf`` / ``fcf_yield`` / ``ocf_yield`` are live price over the
    annual-earnings slice's stored trailing free- (and operating-) cash-flow per share.
    ``price_to_fcf`` is ``null`` for a non-positive FCF (an undefined multiple, like ``pe`` on a
    loss); ``fcf_yield`` / ``ocf_yield`` keep their sign (a negative yield is a real "burning
    cash" reading). The gap between ``ocf_yield`` and ``fcf_yield`` is the capex drag.

    **Profitability & health.** ``gross_margin`` / ``operating_margin`` / ``net_margin`` /
    ``roe`` are percent; ``current_ratio`` and ``debt_to_equity`` are the liquidity / leverage
    ratios; ``beta`` the volatility vs the market — all the fundamentals slice's Yahoo ``.info``
    writes off the anchor.

    **Growth.** ``revenue_growth_yoy`` / ``eps_growth_yoy`` / ``fcf_growth_yoy`` are the *latest
    trailing* YoY growth (newest reported fiscal year over the prior; EPS consensus basis, FCF
    per-share basis); ``forward_revenue_growth_yoy`` / ``forward_eps_growth_yoy`` their forward
    (FY1→FY2 consensus) mirror. All percent, straight off the anchor; ``null`` until the annual
    slice has the years it needs cached (the forward pair the most often, needing two upcoming
    years)."""

    # Valuation
    pe: float | None = None  # trailing: price / TTM EPS (consensus basis, 4 quarters)
    pb: float | None = None  # trailing: price / book value per share
    ps: float | None = None  # trailing: price / sales per share
    peg: float | None = None  # trailing: pe / eps_growth_yoy (consensus basis)
    eps: float | None = None  # trailing TTM EPS (consensus basis), the pe denominator
    forward_pe: float | None = None  # forward: price / FY1 consensus EPS
    forward_ps: float | None = None  # forward: market cap / FY1 consensus revenue
    enterprise_value: float | None = None  # live: price * shares + debt - cash (raw USD)
    ev_ebitda: float | None = None  # live: enterprise value / trailing EBITDA (null if EBITDA <= 0)
    # Cash flow
    price_to_fcf: float | None = None  # trailing: price / FCF per share (null if FCF <= 0)
    fcf_yield: float | None = None  # percent: FCF per share / price (signed)
    ocf_yield: float | None = None  # percent: OCF per share / price (signed; pre-capex)
    # Profitability & health
    gross_margin: float | None = None  # percent
    operating_margin: float | None = None  # percent
    net_margin: float | None = None  # percent
    roe: float | None = None  # percent, return on equity
    current_ratio: float | None = None  # current assets / current liabilities
    debt_to_equity: float | None = None  # total debt / equity (a ratio)
    beta: float | None = None  # volatility vs the market (1.0 = moves with it)
    # Growth
    revenue_growth_yoy: float | None = None  # percent, latest trailing YoY (annual slice)
    eps_growth_yoy: float | None = None  # percent, latest trailing YoY, consensus basis
    fcf_growth_yoy: float | None = None  # percent, latest trailing FCF/share YoY (annual slice)
    forward_revenue_growth_yoy: float | None = None  # percent, forward FY1->FY2 consensus
    forward_eps_growth_yoy: float | None = None  # percent, forward FY1->FY2 consensus


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
    # The extended-hours split (regular close + latest pre/after print), present only outside
    # the regular session; null during it and on the Canadian feed. Lets the FE show the day's
    # move and the after-bell move apart rather than blended into price/change above.
    extended_hours: ExtendedHoursResponse | None = None
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
    mean anything.

    ``signal`` is ``"not_meaningful"`` when the latest release sits on a cyclical earnings trough
    (a near-zero trailing EPS blows the multiple up on a collapsing denominator — Seagate at the
    bottom of a cycle): a percentile read would call it "expensive" when it's mid-cycle cheap, so
    no cheap/fair/expensive verdict is given. ``current_pe`` still carries the real (distorted)
    figure and the band fields describe the rest of the history, so the FE can show the number
    beside a "trailing P/E not meaningful" note."""

    current_pe: float
    median_pe: float
    p25_pe: float
    p75_pe: float
    min_pe: float
    max_pe: float
    current_percentile: float  # 0–100, share of history at or below the current multiple
    discount_to_median_percent: float  # negative = cheaper than its own median
    signal: str  # "cheap" | "fair" | "expensive" | "not_meaningful" (trough earnings)
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