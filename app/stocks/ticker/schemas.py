"""HTTP response DTOs for the ticker endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. That's also why
the renaming is done here: the entity speaks in the domain term ``symbol``; ``ticker``
is this endpoint's JSON vocabulary.

``performance`` reuses the shared ``StockPerformanceResponse`` so those JSON keys stay
one vocabulary across endpoints. ``dividend``, ``performance``, ``metrics`` and
``options_metrics`` are opt-in blocks (the ``include`` query param): ``null`` unless
requested — and for the best-effort ones, ``null`` also when requested but unavailable.
``metrics`` starts with ``forward_peg`` — the one figure no other endpoint serves — and
is where future card-only metrics belong; the PEG's legs (``forward_pe``,
``growth.forward_eps_growth``) deliberately stay snapshot-only so the same numbers
don't get two homes that could disagree. ``options_metrics`` is likewise card-only:
no other endpoint reads the options market.
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

    Two PEGs, side by side: ``peg`` is the trailing read (trailing P/E over
    *already-reported* EPS growth — which a cyclical rebound can inflate and pin
    the ratio near zero), ``forward_peg`` the honest forward cousin (forward P/E
    over the FY1→FY2 growth analysts *expect*); ``forward_peg`` is ``null`` when
    no forward consensus is stored for the symbol yet, or a leg is non-positive.
    The margins are the trailing profitability ladder (percent), from the same
    fundamentals call the market cap rides."""

    peg: float | None = None  # trailing P/E / trailing EPS growth
    forward_peg: float | None = None  # forward P/E / expected FY1->FY2 EPS growth
    gross_margin: float | None = None  # percent
    operating_margin: float | None = None  # percent
    net_margin: float | None = None  # percent


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
    """A ticker's card: the live quote, name and market cap, plus opt-in blocks.

    ``ticker`` is the symbol and ``price``/``change``/``change_percent`` the day's
    move (same rules as every other price view); ``name`` is the clean company
    display name, ``exchange`` the listing venue (served from the ``stocks`` row,
    learned once — it never changes), and ``market_cap`` fundamentals-vendor
    enrichment — all best-effort, ``null`` when unconfigured or unavailable.
    ``dividend``, ``performance``, ``metrics`` and ``options_metrics`` appear
    only when asked for via
    ``?include=`` — ``null`` otherwise, and ``null`` for the best-effort ones even
    when requested if their source is down or keyless."""

    ticker: str
    name: str | None = None  # clean display name ("Micron Technology")
    exchange: str | None = None  # listing venue (e.g. "NASDAQ"); DB-backed
    price: float
    change: float | None = None  # absolute move vs the previous close
    change_percent: float | None = None  # percent move vs the previous close
    market_cap: float | None = None  # raw USD
    dividend: DividendResponse | None = None  # opt-in: ?include=dividend
    performance: StockPerformanceResponse | None = None  # opt-in: ?include=performance
    metrics: TickerMetricsResponse | None = None  # opt-in: ?include=metrics
    options_metrics: OptionsMetricsResponse | None = None  # opt-in: ?include=options_metrics