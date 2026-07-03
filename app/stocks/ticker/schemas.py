"""HTTP response DTO for the ticker endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. That's also why
the renaming is done here: the entity speaks in the domain term ``symbol``; ``ticker``
is this endpoint's JSON vocabulary.

The response is deliberately minimal: ``forward_peg`` is the one figure no other
endpoint serves, and its legs (``forward_pe``, ``growth.forward_eps_growth``) already
ride on the stock snapshot — repeating them here would give the same numbers two homes
that could disagree (two requests price the multiple at different moments). ``price``
is included because the ratio embeds it: it says what quote this PEG was taken at.
"""

from pydantic import BaseModel


class TickerValuationResponse(BaseModel):
    """A ticker's forward PEG at today's price.

    The forward cousin of the snapshot's trailing ``metrics.peg``: forward P/E over
    expected FY1→FY2 EPS growth, dividing by growth analysts *expect* rather than a
    possibly rebound-inflated growth already reported. ``null`` when no forward
    consensus is stored for the symbol yet, or a leg is non-positive (expected loss
    or shrinkage)."""

    ticker: str
    price: float  # the live quote the ratio was computed at
    forward_peg: float | None = None  # forward P/E / expected FY1->FY2 EPS growth
