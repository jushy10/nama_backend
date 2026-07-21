from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


@dataclass(frozen=True)
class VixSnapshot:
    as_of: date
    value: float
    previous_close: float | None = None

    @property
    def change(self) -> float | None:
        if self.previous_close is None:
            return None
        return round(self.value - self.previous_close, 2)

    @property
    def change_percent(self) -> float | None:
        if self.previous_close is None or self.previous_close == 0:
            return None
        return round((self.value - self.previous_close) / self.previous_close * 100, 2)

    @property
    def regime(self) -> str:
        v = self.value
        if v < 15:
            return "low"
        if v < 20:
            return "normal"
        if v < 30:
            return "elevated"
        if v < 40:
            return "high"
        return "extreme"


class FearGreedBand(str, Enum):
    EXTREME_FEAR = "extreme_fear"
    FEAR = "fear"
    NEUTRAL = "neutral"
    GREED = "greed"
    EXTREME_GREED = "extreme_greed"

    @classmethod
    def from_score(cls, score: float) -> "FearGreedBand":
        if score < 25:
            return cls.EXTREME_FEAR
        if score < 45:
            return cls.FEAR
        if score <= 55:
            return cls.NEUTRAL
        if score <= 75:
            return cls.GREED
        return cls.EXTREME_GREED

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


@dataclass(frozen=True)
class FearGreedSnapshot:
    score: float
    as_of: datetime
    rating: str = ""
    previous_close: float | None = None
    previous_1_week: float | None = None
    previous_1_month: float | None = None
    previous_1_year: float | None = None

    @property
    def band(self) -> FearGreedBand:
        return FearGreedBand.from_score(self.score)

    @property
    def label(self) -> str:
        return self.band.label


@dataclass(frozen=True)
class MarketSentiment:
    vix: VixSnapshot | None = None
    fear_greed: FearGreedSnapshot | None = None
