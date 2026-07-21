from datetime import date

from pydantic import BaseModel


class AnalystPriceTargetsResponse(BaseModel):
    mean: float | None = None
    high: float | None = None
    low: float | None = None
    median: float | None = None


class RecommendationTrendResponse(BaseModel):
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
    firm: str
    published_at: date  # ISO date the action was published
    action: str | None = None
    from_grade: str | None = None
    to_grade: str | None = None
    target_current: float | None = None
    target_prior: float | None = None
    is_upgrade: bool
    is_downgrade: bool


class TopFirmRatingResponse(BaseModel):
    firm: str
    rank: int
    rating: str | None = None
    action: str | None = None
    target: float | None = None
    published_at: date  # ISO date the firm last acted


class AnalystRecommendationsBlock(BaseModel):
    direction: str | None = None
    latest: RecommendationTrendResponse | None = None
    price_targets: AnalystPriceTargetsResponse | None = None
    trends: list[RecommendationTrendResponse]


class AnalystInfoResponse(BaseModel):
    ticker: str
    recommendations: AnalystRecommendationsBlock
    rating_changes: list[RatingChangeResponse]
    top_firms: list[TopFirmRatingResponse]
