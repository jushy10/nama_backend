import datetime

from pydantic import BaseModel


class VixResponse(BaseModel):
    as_of: datetime.date
    value: float
    previous_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    # low / normal / elevated / high / extreme
    regime: str


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


class MarketSentimentResponse(BaseModel):
    vix: VixResponse | None = None
    fear_greed: FearGreedResponse | None = None
