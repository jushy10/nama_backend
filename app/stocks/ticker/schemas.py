"""HTTP response DTOs for the ticker endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. That's also why
the renaming is done here: the entity speaks in the domain term ``symbol``; ``ticker``
is this endpoint's JSON vocabulary.

``performance`` reuses the shared ``StockPerformanceResponse`` so the ``1w``/``1m``/…
JSON keys stay one vocabulary across endpoints. ``metrics`` is this endpoint's own
block: it starts with ``forward_peg`` — the one figure no other endpoint serves — and
is where future card-only metrics belong. The PEG's legs (``forward_pe``,
``growth.forward_eps_growth``) deliberately stay snapshot-only so the same numbers
don't get two homes that could disagree.
"""

from pydantic import BaseModel

from app.stocks.schemas import StockPerformanceResponse


class TickerMetricsResponse(BaseModel):
    """The card's derived metrics — currently the forward PEG.

    The forward cousin of the snapshot's trailing ``metrics.peg``: forward P/E over
    expected FY1→FY2 EPS growth, dividing by growth analysts *expect* rather than a
    possibly rebound-inflated growth already reported. ``null`` when no forward
    consensus is stored for the symbol yet, or a leg is non-positive (expected loss
    or shrinkage)."""

    forward_peg: float | None = None  # forward P/E / expected FY1->FY2 EPS growth


class TickerCardResponse(BaseModel):
    """A ticker's card: the live quote, valuation metrics, and enrichment.

    ``ticker`` is the symbol and ``price``/``change``/``change_percent`` the day's
    move (same rules as every other price view). ``name`` is the clean company
    display name from the profile vendor, ``market_cap`` and the dividend fields
    are fundamentals-vendor enrichment, and ``performance`` the trailing return
    windows — all best-effort, ``null`` when their source is unconfigured or
    unavailable. ``metrics`` carries the card's derived figures (forward PEG)."""

    ticker: str
    name: str | None = None  # clean display name ("Micron Technology")
    price: float
    change: float | None = None  # absolute move vs the previous close
    change_percent: float | None = None  # percent move vs the previous close
    market_cap: float | None = None  # raw USD
    dividend_per_share: float | None = None  # $ per share, annual
    dividend_yield: float | None = None  # percent
    performance: StockPerformanceResponse | None = None  # 1w/1m/3m/6m/ytd/1y (percent)
    metrics: TickerMetricsResponse  # derived figures; fields null without coverage
