"""HTTP response DTOs for the recommendations endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. ``total``,
``score``, ``consensus``, and ``direction`` are surfaced as plain fields (they're
computed on the entity) so a client doesn't have to re-derive them.
"""

from datetime import date

from pydantic import BaseModel


class RecommendationTrendResponse(BaseModel):
    """Analysts' buy/hold/sell split for one monthly snapshot.

    The five buckets are the analyst counts for each stance; ``total`` sums them,
    ``score`` is the consensus mean on the 1 (Strong Buy) … 5 (Strong Sell) scale
    (``null`` with no coverage), and ``consensus`` that mean as a five-step
    label (``Strong Buy`` … ``Strong Sell``)."""

    period: date  # first day of the month the snapshot covers
    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int
    total: int
    score: float | None = None
    consensus: str | None = None


class RecommendationsResponse(BaseModel):
    """Analyst recommendation trends for a symbol, newest snapshot first.

    The forward "what does the street think?" read for the stock page.
    ``latest`` is the current month's split and ``direction`` how the consensus
    shifted from the prior month ("upgraded" / "downgraded" / "unchanged" /
    ``null``) — the predictive part. ``count`` is how many monthly snapshots are
    returned; an empty ``trends`` means no analyst covers the symbol."""

    symbol: str
    count: int
    direction: str | None = None
    latest: RecommendationTrendResponse | None = None
    trends: list[RecommendationTrendResponse]
