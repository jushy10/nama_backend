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
    no forward consensus is stored for the symbol yet, or a leg is
    non-positive. The margins are the trailing profitability ladder (percent),
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