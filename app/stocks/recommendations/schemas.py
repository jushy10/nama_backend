"""HTTP response DTOs for the analyst-info endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. ``total``,
``score``, ``consensus``, and ``direction`` are surfaced as plain fields (they're
computed on the entity) so a client doesn't have to re-derive them.

``AnalystInfoResponse`` is the one response the ``GET /stocks/ticker/{ticker}/analyst-info``
endpoint serves: the recommendation trends (+ price targets) in a nested block beside the
discrete rating-change events. The inner ``RecommendationTrendResponse`` /
``AnalystPriceTargetsResponse`` / ``RatingChangeResponse`` shapes are the reusable pieces it's
built from.
"""

from datetime import date

from pydantic import BaseModel


class AnalystPriceTargetsResponse(BaseModel):
    """The consensus 12-month price target for the stock — the sell-side's ``mean``/``median``
    view and the ``high``/``low`` range across estimates. Every field is ``null`` when the
    source serves no target; the whole block is ``null`` when there's no coverage at all."""

    mean: float | None = None
    high: float | None = None
    low: float | None = None
    median: float | None = None


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


class RatingChangeResponse(BaseModel):
    """One published sell-side rating action — the discrete event behind the trend.

    ``firm`` and ``published_at`` identify it; ``action`` is Yahoo's grade action
    (``up``/``down``/``init``/``main``/``reit``), ``from_grade``→``to_grade`` the move,
    and ``target_current``/``target_prior`` the price target it set vs. the one it
    replaced (any of these ``null`` when the source omits it). ``is_upgrade``/
    ``is_downgrade`` surface the direction so a client doesn't re-derive it from ``action``."""

    firm: str
    published_at: date  # ISO date the action was published
    action: str | None = None
    from_grade: str | None = None
    to_grade: str | None = None
    target_current: float | None = None
    target_prior: float | None = None
    is_upgrade: bool
    is_downgrade: bool


class AnalystRecommendationsBlock(BaseModel):
    """The recommendation-trend half of the analyst-info card.

    The monthly buy/hold/sell series (``trends``, newest snapshot first), the current
    consensus (``latest``) and how it shifted from the prior month (``direction`` —
    "upgraded" / "downgraded" / "unchanged" / ``null``), plus the current consensus 12-month
    ``price_targets`` (``null`` when the source serves none). An empty ``trends`` means no
    analyst covers the symbol."""

    direction: str | None = None
    latest: RecommendationTrendResponse | None = None
    price_targets: AnalystPriceTargetsResponse | None = None
    trends: list[RecommendationTrendResponse]


class AnalystInfoResponse(BaseModel):
    """A stock's full analyst coverage in one payload — the response of
    ``GET /stocks/ticker/{ticker}/analyst-info``.

    ``recommendations`` is the buy/hold/sell trend block (+ consensus + price targets);
    ``rating_changes`` is the discrete upgrade/downgrade event feed, newest first — the
    individual actions that, aggregated by month, become the trend. Both are best-effort: an
    uncovered stock is a 200 with an empty ``trends`` and an empty ``rating_changes``, never a
    404."""

    ticker: str
    recommendations: AnalystRecommendationsBlock
    rating_changes: list[RatingChangeResponse]
