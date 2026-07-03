"""HTTP response DTOs for the ticker endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. That's also why
the renaming is done here: the entity speaks in the domain term ``symbol``; ``ticker``
is this endpoint's JSON vocabulary.

``performance`` reuses the shared ``StockPerformanceResponse`` so those JSON keys stay
one vocabulary across endpoints. ``dividend``, ``performance`` and ``metrics`` are
opt-in blocks (the ``include`` query param): ``null`` unless requested — and for the
best-effort ones, ``null`` also when requested but unavailable. ``metrics`` starts
with ``forward_peg`` — the one figure no other endpoint serves — and is where future
card-only metrics belong; the PEG's legs (``forward_pe``, ``growth.forward_eps_growth``)
deliberately stay snapshot-only so the same numbers don't get two homes that could
disagree.
"""

from pydantic import BaseModel

from app.stocks.schemas import StockPerformanceResponse


class DividendResponse(BaseModel):
    """The stock's dividend, as the fundamentals vendor reports it.

    ``yield_percentage`` is percent; ``per_share`` is the annual payout in the
    quote currency. Either is ``null`` for a non-payer or an uncovered field."""

    yield_percentage: float | None = None  # percent
    per_share: float | None = None  # $ per share, annual


class TickerMetricsResponse(BaseModel):
    """The card's derived metrics — currently the forward PEG.

    The forward cousin of the snapshot's trailing ``metrics.peg``: forward P/E over
    expected FY1→FY2 EPS growth, dividing by growth analysts *expect* rather than a
    possibly rebound-inflated growth already reported. ``null`` when no forward
    consensus is stored for the symbol yet, or a leg is non-positive (expected loss
    or shrinkage)."""

    forward_peg: float | None = None  # forward P/E / expected FY1->FY2 EPS growth


class TickerCardResponse(BaseModel):
    """A ticker's card: the live quote, name and market cap, plus opt-in blocks.

    ``ticker`` is the symbol and ``price``/``change``/``change_percent`` the day's
    move (same rules as every other price view); ``name`` is the clean company
    display name and ``market_cap`` fundamentals-vendor enrichment (best-effort,
    ``null`` when unconfigured or unavailable). ``dividend``, ``performance`` and
    ``metrics`` appear only when asked for via ``?include=`` — ``null`` otherwise,
    and ``null`` for the best-effort ones even when requested if their source is
    down or keyless."""

    ticker: str
    name: str | None = None  # clean display name ("Micron Technology")
    price: float
    change: float | None = None  # absolute move vs the previous close
    change_percent: float | None = None  # percent move vs the previous close
    market_cap: float | None = None  # raw USD
    dividend: DividendResponse | None = None  # opt-in: ?include=dividend
    performance: StockPerformanceResponse | None = None  # opt-in: ?include=performance
    metrics: TickerMetricsResponse | None = None  # opt-in: ?include=metrics