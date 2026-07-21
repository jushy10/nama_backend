from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.stocks.entities import StockPerformance


class Recommendation(str, Enum):
    STRONG_BUY = "strong_buy"
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"
    STRONG_SELL = "strong_sell"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class InvestmentAnalysis:
    symbol: str
    recommendation: Recommendation
    confidence: Confidence
    thesis: str
    strengths: tuple[str, ...]
    risks: tuple[str, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        return bool(self.strengths and self.risks)


class SectionStance(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


@dataclass(frozen=True)
class SectionMetric:
    label: str
    value: str


@dataclass(frozen=True)
class ScorecardSection:
    key: str
    title: str
    stance: SectionStance
    label: str
    summary: str
    metrics: tuple[SectionMetric, ...] = ()


@dataclass(frozen=True)
class StockScorecard:
    symbol: str
    recommendation: Recommendation
    confidence: Confidence
    thesis: str
    sections: tuple[ScorecardSection, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        return bool(self.sections) and all(s.label and s.summary for s in self.sections)


class MarketTone(str, Enum):
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    MIXED = "mixed"


@dataclass(frozen=True)
class SectorMover:
    ticker: str
    name: str | None
    change_percent: float | None
    market_cap: float | None

    @property
    def weighted_move(self) -> float | None:
        if self.change_percent is None or self.market_cap is None:
            return None
        return self.market_cap * self.change_percent


@dataclass(frozen=True)
class SectorBreadth:
    advancers: int
    decliners: int
    total: int


@dataclass(frozen=True)
class SectorHeadline:
    ticker: str
    title: str
    published_at: datetime | None = None
    publisher: str | None = None
    link: str | None = None


@dataclass(frozen=True)
class SectorContext:
    sector: str
    symbol: str  # the proxy ETF ticker, carried through from the board
    change_percent: float | None
    performance: StockPerformance | None = None
    movers: tuple[SectorMover, ...] = ()
    breadth: SectorBreadth | None = None
    headlines: tuple[SectorHeadline, ...] = ()

    @classmethod
    def from_constituents(
        cls,
        *,
        sector: str,
        symbol: str,
        change_percent: float | None,
        performance: StockPerformance | None,
        constituents: "tuple[SectorMover, ...]",
        top_n: int = 3,
    ) -> "SectorContext":
        advancers = tuple(m for m in constituents if (m.change_percent or 0) > 0)
        decliners = tuple(m for m in constituents if (m.change_percent or 0) < 0)
        quoted = tuple(m for m in constituents if m.change_percent is not None)
        # Rank by cap-weighted contribution magnitude; a missing weight sorts last.
        gainers = sorted(
            advancers, key=lambda m: m.weighted_move or 0.0, reverse=True
        )
        losers = sorted(decliners, key=lambda m: m.weighted_move or 0.0)
        leading = change_percent is None or change_percent >= 0
        movers = tuple((gainers if leading else losers)[:top_n])
        # No quoted members (attribution unavailable, or the sector had no readable
        # constituents) -> no breadth, rather than a meaningless "0 of 0".
        breadth = (
            SectorBreadth(len(advancers), len(decliners), len(quoted)) if quoted else None
        )
        return cls(
            sector=sector,
            symbol=symbol,
            change_percent=change_percent,
            performance=performance,
            movers=movers,
            breadth=breadth,
        )


@dataclass(frozen=True)
class SectorHighlight:
    sector: str
    symbol: str  # the proxy ETF ticker, carried through from the board
    change_percent: float | None
    note: str
    movers: tuple[SectorMover, ...] = ()
    headlines: tuple[SectorHeadline, ...] = ()


@dataclass(frozen=True)
class SectorAnalysis:
    summary: str
    tone: MarketTone
    leaders: tuple[SectorHighlight, ...]
    laggards: tuple[SectorHighlight, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        return bool(self.summary and (self.leaders or self.laggards))


class MarketPeriod(str, Enum):
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


@dataclass(frozen=True)
class MarketIndexReturn:
    name: str
    symbol: str  # the proxy ETF ticker, carried through from the board
    change_percent: float | None


@dataclass(frozen=True)
class MarketPeriodHighlight:
    period: MarketPeriod
    note: str
    indexes: tuple[MarketIndexReturn, ...]


@dataclass(frozen=True)
class MarketSummary:
    summary: str
    tone: MarketTone
    periods: tuple[MarketPeriodHighlight, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        return bool(self.summary and self.periods)


class EarningsTrend(str, Enum):
    ACCELERATING = "accelerating"
    STEADY = "steady"
    SLOWING = "slowing"


@dataclass(frozen=True)
class EarningsAnalysis:
    symbol: str
    summary: str
    trend: EarningsTrend
    highlights: tuple[str, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        return bool(self.summary and self.highlights)


class RatingsVerdict(str, Enum):
    BULLISH = "bullish"
    MIXED = "mixed"
    CAUTIOUS = "cautious"


@dataclass(frozen=True)
class RatingsAnalysis:
    symbol: str
    verdict: RatingsVerdict
    confidence: Confidence
    summary: str
    findings: tuple[str, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        return bool(self.summary and self.findings)


class FundamentalsVerdict(str, Enum):
    STRONG = "strong"
    MIXED = "mixed"
    WEAK = "weak"


@dataclass(frozen=True)
class FundamentalsAnalysis:
    symbol: str
    verdict: FundamentalsVerdict
    confidence: Confidence
    summary: str
    findings: tuple[str, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        return bool(self.summary and self.findings)
