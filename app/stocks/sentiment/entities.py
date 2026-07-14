"""Enterprise Business Rules: the market-sentiment slice's own entities.

The home page's two at-a-glance "market mood" reads: the **VIX** (CBOE's
volatility index — the market's "fear gauge") and the **CNN Fear & Greed Index**
(a 0–100 composite sentiment score). Pure domain objects — frozen dataclasses
that import nothing from the outer layers.

The facts *about* each read live here as computed properties, never stored: the
VIX's day-over-day change and its volatility *regime* band, and the Fear & Greed
score's canonical band + human label. Classification thresholds are the domain's
own — the CNN adapter carries CNN's raw ``rating`` string alongside, but the band
we surface is derived here so one place owns the rule.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


@dataclass(frozen=True)
class VixSnapshot:
    """The CBOE Volatility Index (VIX) on one date, with the prior close.

    ``value`` and ``previous_close`` are index points (a VIX of 17.16 means
    17.16). ``previous_close`` is the immediately preceding trading day's close,
    kept so the day-over-day ``change`` is a fact the entity computes rather than
    something a caller has to derive.
    """

    as_of: date
    value: float
    previous_close: float | None = None

    @property
    def change(self) -> float | None:
        """Points moved since the previous close, or None if it's unknown."""
        if self.previous_close is None:
            return None
        return round(self.value - self.previous_close, 2)

    @property
    def change_percent(self) -> float | None:
        """Percent moved since the previous close, or None if it's unknown."""
        if self.previous_close is None or self.previous_close == 0:
            return None
        return round((self.value - self.previous_close) / self.previous_close * 100, 2)

    @property
    def regime(self) -> str:
        """The volatility regime the level sits in (rough market convention).

        low (<15) · normal (15–20) · elevated (20–30) · high (30–40) ·
        extreme (40+). A lowercase token so the client owns the display label
        and colour, the same way the sentiment tone is surfaced.
        """
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
    """The five canonical CNN Fear & Greed bands, keyed off the 0–100 score."""

    EXTREME_FEAR = "extreme_fear"
    FEAR = "fear"
    NEUTRAL = "neutral"
    GREED = "greed"
    EXTREME_GREED = "extreme_greed"

    @classmethod
    def from_score(cls, score: float) -> "FearGreedBand":
        """Map a 0–100 score onto its band using CNN's published thresholds.

        0–24 extreme fear · 25–44 fear · 45–55 neutral · 56–75 greed ·
        76–100 extreme greed.
        """
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
        """Human label, e.g. ``extreme_fear`` -> ``"Extreme Fear"``."""
        return self.value.replace("_", " ").title()


@dataclass(frozen=True)
class FearGreedSnapshot:
    """The CNN Fear & Greed Index right now, with its trailing comparisons.

    ``score`` is 0–100 (higher = greedier). ``rating`` is CNN's own label for the
    current score, carried verbatim; the ``band``/``label`` we surface are derived
    from the score here so the classification rule lives in one place. The four
    ``previous_*`` values are the score at each trailing horizon, for a
    then-vs-now read (any may be absent).
    """

    score: float
    as_of: datetime
    rating: str = ""
    previous_close: float | None = None
    previous_1_week: float | None = None
    previous_1_month: float | None = None
    previous_1_year: float | None = None

    @property
    def band(self) -> FearGreedBand:
        """The canonical band the current score falls in."""
        return FearGreedBand.from_score(self.score)

    @property
    def label(self) -> str:
        """Human band label, e.g. ``"Extreme Fear"``."""
        return self.band.label


@dataclass(frozen=True)
class MarketSentiment:
    """The combined home-page read: the VIX and the Fear & Greed score.

    Each leg is independently optional — the two come from different keyless
    sources, so one being unavailable must not blank the other. A snapshot with
    *both* legs missing is never constructed (the use case raises instead).
    """

    vix: VixSnapshot | None = None
    fear_greed: FearGreedSnapshot | None = None
