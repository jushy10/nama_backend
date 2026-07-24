from datetime import date

from pydantic import BaseModel

from app.domains.coverage.recommendations.entities import (
    AnalystInfo,
    AnalystPriceTargets,
    AnalystRecommendations,
    FirmRating,
    RatingChange,
    RecommendationTrend,
)


class AnalystPriceTargetsResponse(BaseModel):
    mean: float | None = None
    high: float | None = None
    low: float | None = None
    median: float | None = None

    @classmethod
    def from_targets(cls, targets: AnalystPriceTargets) -> "AnalystPriceTargetsResponse":
        return cls(
            mean=targets.mean,
            high=targets.high,
            low=targets.low,
            median=targets.median,
        )


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

    @classmethod
    def from_trend(cls, trend: RecommendationTrend) -> "RecommendationTrendResponse":
        return cls(
            period=trend.period,
            strong_buy=trend.strong_buy,
            buy=trend.buy,
            hold=trend.hold,
            sell=trend.sell,
            strong_sell=trend.strong_sell,
            total=trend.total,
            score=trend.score,
            consensus=trend.consensus,
        )


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

    @classmethod
    def from_change(cls, change: RatingChange) -> "RatingChangeResponse":
        return cls(
            firm=change.firm,
            published_at=change.published_at,
            action=change.action,
            from_grade=change.from_grade,
            to_grade=change.to_grade,
            target_current=change.target_current,
            target_prior=change.target_prior,
            is_upgrade=change.is_upgrade,
            is_downgrade=change.is_downgrade,
        )


class TopFirmRatingResponse(BaseModel):
    firm: str
    rank: int
    rating: str | None = None
    action: str | None = None
    target: float | None = None
    published_at: date  # ISO date the firm last acted

    @classmethod
    def from_firm(cls, firm: FirmRating) -> "TopFirmRatingResponse":
        return cls(
            firm=firm.firm,
            rank=firm.rank,
            rating=firm.rating,
            action=firm.action,
            target=firm.target,
            published_at=firm.published_at,
        )


class AnalystRecommendationsBlock(BaseModel):
    direction: str | None = None
    latest: RecommendationTrendResponse | None = None
    price_targets: AnalystPriceTargetsResponse | None = None
    trends: list[RecommendationTrendResponse]

    @classmethod
    def from_recommendations(
        cls, recs: AnalystRecommendations
    ) -> "AnalystRecommendationsBlock":
        latest = recs.latest
        targets = recs.price_targets
        return cls(
            direction=recs.direction,
            latest=RecommendationTrendResponse.from_trend(latest) if latest else None,
            price_targets=(
                AnalystPriceTargetsResponse.from_targets(targets) if targets else None
            ),
            trends=[RecommendationTrendResponse.from_trend(t) for t in recs.trends],
        )


class AnalystInfoResponse(BaseModel):
    ticker: str
    recommendations: AnalystRecommendationsBlock
    rating_changes: list[RatingChangeResponse]
    top_firms: list[TopFirmRatingResponse]

    @classmethod
    def from_info(cls, info: AnalystInfo) -> "AnalystInfoResponse":
        return cls(
            ticker=info.symbol,
            recommendations=AnalystRecommendationsBlock.from_recommendations(
                info.recommendations
            ),
            rating_changes=[
                RatingChangeResponse.from_change(c) for c in info.rating_changes.changes
            ],
            top_firms=[TopFirmRatingResponse.from_firm(f) for f in info.top_firms],
        )
