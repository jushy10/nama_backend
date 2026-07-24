import datetime

from pydantic import BaseModel

from app.domains.macro.sentiment.entities import (
    FearGreedSnapshot,
    MarketSentiment,
    VixSnapshot,
)


class VixResponse(BaseModel):
    as_of: datetime.date
    value: float
    previous_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    # low / normal / elevated / high / extreme
    regime: str

    @classmethod
    def from_snapshot(cls, vix: VixSnapshot) -> "VixResponse":
        return cls(
            as_of=vix.as_of,
            value=vix.value,
            previous_close=vix.previous_close,
            change=vix.change,
            change_percent=vix.change_percent,
            regime=vix.regime,
        )


class FearGreedResponse(BaseModel):
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

    @classmethod
    def from_snapshot(cls, fear_greed: FearGreedSnapshot) -> "FearGreedResponse":
        return cls(
            score=fear_greed.score,
            as_of=fear_greed.as_of,
            rating=fear_greed.rating,
            band=fear_greed.band.value,
            label=fear_greed.label,
            previous_close=fear_greed.previous_close,
            previous_1_week=fear_greed.previous_1_week,
            previous_1_month=fear_greed.previous_1_month,
            previous_1_year=fear_greed.previous_1_year,
        )


class MarketSentimentResponse(BaseModel):
    vix: VixResponse | None = None
    fear_greed: FearGreedResponse | None = None

    @classmethod
    def from_sentiment(cls, sentiment: MarketSentiment) -> "MarketSentimentResponse":
        return cls(
            vix=(
                VixResponse.from_snapshot(sentiment.vix)
                if sentiment.vix is not None
                else None
            ),
            fear_greed=(
                FearGreedResponse.from_snapshot(sentiment.fear_greed)
                if sentiment.fear_greed is not None
                else None
            ),
        )
