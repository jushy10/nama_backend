"""Enterprise Business Rules: the AI-analysis slice's own entities.

Every AI-generated read the API serves — the per-stock buy/hold/sell analysis
(shared with the ETF analyser), the earnings story, the analyst-coverage
review, the sector rotation read, and the market summary. Pure domain objects:
the model fills in the substance, the presenter attaches the disclaimer at the
edge, and ``model``/``generated_at`` keep every read traceable.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Recommendation(str, Enum):
    """The headline buy / hold / sell call of an AI stock analysis.

    The string values double as the JSON the model returns and the API serves,
    the same convention as ``Timeframe``.
    """

    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"


class Confidence(str, Enum):
    """How firmly an analysis holds its recommendation, given the data."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class InvestmentAnalysis:
    """An AI-generated, balanced read on whether a stock looks like a buy.

    Produced by a language model from the figures the rest of the slice already
    gathers — the price snapshot, trailing performance, the valuation/health
    metrics, and the recent earnings beat history — never from outside data the
    model happens to recall. It is informational, not personalized financial
    advice: the model fills in the substance below, and the edge (the presenter)
    is what attaches the disclaimer.

    ``recommendation`` is the headline call and ``confidence`` how firmly it's
    held; ``thesis`` is a few sentences of reasoning, with ``strengths`` (the
    bull case) and ``risks`` (the bear case) as short bullet points. ``model``
    records which model produced it and ``generated_at`` when, so a cached or
    stored analysis stays traceable.
    """

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
        """Whether the read carries its full substance — both the bull case
        (``strengths``) and the bear case (``risks``). The AI-analysis use cases
        refuse to cache an analysis that isn't complete, so a rare empty-list model
        result is never frozen for the cache TTL; the next view regenerates."""
        return bool(self.strengths and self.risks)


class MarketTone(str, Enum):
    """The risk posture the day's sector rotation implies.

    A day where cyclical/growth sectors (tech, discretionary) lead is ``risk_on``
    (appetite for risk); one where defensives (staples, utilities, health care)
    lead is ``risk_off`` (a flight to safety); no clear rotation is ``mixed``. The
    string values double as the JSON the model returns and the API serves, the
    same convention as ``Recommendation``.
    """

    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    MIXED = "mixed"


@dataclass(frozen=True)
class SectorHighlight:
    """One sector called out in a market analysis, with the model's plain note.

    ``change_percent`` is *not* authored by the model — it's joined back from the
    day's board (matched to the sector the model named) so the number on the card
    stays a real quote, never a figure the model invented. ``note`` is the model's
    one-line, plain-language read on why the sector is leading or lagging.
    """

    sector: str
    symbol: str  # the proxy ETF ticker, carried through from the board
    change_percent: float | None
    note: str


@dataclass(frozen=True)
class SectorAnalysis:
    """An AI-generated read on how the market's sectors are moving today.

    The market-wide sibling of ``InvestmentAnalysis``: produced by a language
    model from the day's ranked sector board (each sector's move + trailing
    returns) and nothing else — never outside data the model happens to recall.
    ``summary`` is the plain-language headline of which corners of the market are
    leading and lagging; ``tone`` is the risk posture that rotation implies;
    ``leaders`` and ``laggards`` name the standout sectors with a short note each
    (their ``change_percent`` joined back from the board, not authored). It is
    informational, not personalized advice — the model fills in the substance and
    the presenter attaches the disclaimer. ``model``/``generated_at`` keep a
    cached read traceable, as with ``InvestmentAnalysis``.
    """

    summary: str
    tone: MarketTone
    leaders: tuple[SectorHighlight, ...]
    laggards: tuple[SectorHighlight, ...]
    model: str
    generated_at: datetime


class MarketPeriod(str, Enum):
    """A trailing timeframe the market summary reads over — the past week, month,
    or year.

    The string values double as the JSON the model returns and the API serves,
    the same convention as ``MarketTone``.
    """

    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


@dataclass(frozen=True)
class MarketIndexReturn:
    """One index's return over a single timeframe, carried on a period highlight.

    ``change_percent`` is joined from the day's board (a real quote), never
    authored by the model — the same discipline ``SectorHighlight`` follows.
    """

    name: str
    symbol: str  # the proxy ETF ticker, carried through from the board
    change_percent: float | None


@dataclass(frozen=True)
class MarketPeriodHighlight:
    """One timeframe (past week/month/year) in the market summary.

    ``note`` is the model's one-line, plain-language read of how that stretch
    went; ``indexes`` carries each index's real return over the window, joined
    from the board (real quotes) rather than authored by the model.
    """

    period: MarketPeriod
    note: str
    indexes: tuple[MarketIndexReturn, ...]


@dataclass(frozen=True)
class MarketSummary:
    """An AI-generated overview of how the US market has moved lately.

    The market-wide sibling of ``SectorAnalysis``: produced by a language model
    from the day's index board (the S&P 500 and the Nasdaq, each with its
    trailing-window returns) and nothing else — never outside data the model
    happens to recall. ``summary`` is the plain-language headline; ``tone`` is the
    risk posture the recent moves imply; ``periods`` breaks the read down by
    timeframe (the past year, month and week), each with a short note and the
    indexes' real returns (joined from the board, not authored). It is
    informational, not personalized advice — the model fills in the substance and
    the presenter attaches the disclaimer. ``model``/``generated_at`` keep a read
    traceable, as with ``SectorAnalysis``.
    """

    summary: str
    tone: MarketTone
    periods: tuple[MarketPeriodHighlight, ...]
    model: str
    generated_at: datetime


class EarningsTrend(str, Enum):
    """Where a company's earnings story is heading.

    ``accelerating`` when profit/sales growth is picking up (or its beats are
    getting bigger), ``slowing`` when growth is fading or it's starting to miss,
    ``steady`` when it's holding a consistent pace. The string values double as
    the JSON the model returns and the API serves, the same convention as
    ``Recommendation`` and ``MarketTone``.
    """

    ACCELERATING = "accelerating"
    STEADY = "steady"
    SLOWING = "slowing"


@dataclass(frozen=True)
class EarningsAnalysis:
    """An AI-generated, plain-language read of a company's earnings story.

    The earnings-focused sibling of ``InvestmentAnalysis``: produced by a language
    model from the earnings figures the slice already gathers — the recent
    quarterly and annual timelines (beats/misses, EPS and revenue, and the forward
    consensus) — never from outside data the model happens to recall. ``summary``
    is the plain-language headline of how earnings have gone and where they look
    headed; ``trend`` is the direction; ``highlights`` are a few short takeaways.
    It is informational, not personalized advice — the model fills in the substance
    and the presenter attaches the disclaimer. ``model``/``generated_at`` keep a
    read traceable, as with ``InvestmentAnalysis``.
    """

    symbol: str
    summary: str
    trend: EarningsTrend
    highlights: tuple[str, ...]
    model: str
    generated_at: datetime


class RatingsVerdict(str, Enum):
    """The overall read of a stock's analyst coverage.

    ``bullish`` when the sell-side leans clearly positive (a lopsided Buy/Overweight split,
    rising targets, upgrades), ``cautious`` when it leans negative or is deteriorating (Holds and
    Sells, downgrades, falling targets), ``mixed`` when it's split or sending conflicting signals.
    The string values double as the JSON the model returns and the API serves, the same
    convention as ``EarningsTrend``.
    """

    BULLISH = "bullish"
    MIXED = "mixed"
    CAUTIOUS = "cautious"


@dataclass(frozen=True)
class RatingsAnalysis:
    """An AI-generated, plain-language read of a stock's *analyst coverage*.

    The analyst-ratings sibling of ``EarningsAnalysis``: produced by a language model from the
    coverage the recommendations slice already gathers — the buy/hold/sell consensus and how it's
    shifting, the consensus price target and its spread, and the most credible covering firms'
    current stances — never from outside data the model happens to recall. ``verdict`` is the
    overall read and ``confidence`` how firmly it's held; ``summary`` is the plain-language
    headline and ``findings`` a few short, concrete takeaways (e.g. a lopsided Buy split, a wide
    target spread, the most credible firms sitting below consensus). Informational, not advice —
    the model fills in the substance and the presenter attaches the disclaimer.
    ``model``/``generated_at`` keep a read traceable, as with ``EarningsAnalysis``.
    """

    symbol: str
    verdict: RatingsVerdict
    confidence: Confidence
    summary: str
    findings: tuple[str, ...]
    model: str
    generated_at: datetime
