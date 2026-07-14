"""HTTP response models for the market-sentiment endpoint.

Pydantic is a web/serialization detail, so these DTOs live at the edge —
deliberately separate from the entities so the core stays framework-agnostic.
The derived reads (the VIX change + regime, the Fear & Greed band + label) are
surfaced top-level so a client doesn't recompute what the entity already knows.
Both legs are optional: each source is best-effort, so a response can carry one,
the other, or both.
"""

import datetime

from pydantic import BaseModel


class VixResponse(BaseModel):
    """The current VIX close with its day-over-day change and volatility regime."""

    as_of: datetime.date
    value: float
    previous_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    # low / normal / elevated / high / extreme
    regime: str


class FearGreedResponse(BaseModel):
    """The current CNN Fear & Greed score, band, and trailing comparisons."""

    score: float
    as_of: datetime.datetime
    # CNN's own label for the current score, carried verbatim.
    rating: str
    # The canonical band we derive from the score, and its human label.
    band: str
    label: str
    previous_close: float | None = None
    previous_1_week: float | None = None
    previous_1_month: float | None = None
    previous_1_year: float | None = None


class MarketSentimentResponse(BaseModel):
    """The combined home-page read: the VIX and the Fear & Greed score.

    Either leg may be ``null`` when its source is unavailable; the endpoint only
    fails outright (502) when both are missing.
    """

    vix: VixResponse | None = None
    fear_greed: FearGreedResponse | None = None
