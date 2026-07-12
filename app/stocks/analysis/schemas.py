"""HTTP response models for the AI-analysis endpoints.

Pydantic is a web/serialization detail, so these DTOs live at the edge —
deliberately separate from the entities so the core stays framework-agnostic.
"""

from datetime import datetime

from pydantic import BaseModel


class SectionMetricResponse(BaseModel):
    """One supporting figure under a scorecard section — a ``label`` and a
    pre-formatted display ``value`` (e.g. ``"Net margin"`` / ``"25.00%"``). Attached
    by the service from gathered data, never authored by the model."""

    label: str
    value: str


class ScorecardSectionResponse(BaseModel):
    """One graded facet of the stock scorecard.

    ``key`` is a stable id the client renders off (``business_quality`` /
    ``valuation`` / ``earnings`` / ``analyst_view``); ``title`` its display name.
    ``stance`` is the favourability signal ("positive"/"neutral"/"negative", the
    client colours on it), ``label`` a short human tag, ``summary`` a plain-language
    read, and ``metrics`` the supporting chips."""

    key: str
    title: str
    stance: str  # "positive" | "neutral" | "negative"
    label: str
    summary: str
    metrics: list[SectionMetricResponse]


class InvestmentAnalysisResponse(BaseModel):
    """An AI-generated, **sectioned** buy/hold/sell scorecard for a stock.

    ``recommendation`` is the overall call on the five-point scale
    ("strong_buy"/"buy"/"hold"/"sell"/"strong_sell"); ``confidence``
    ("low"/"medium"/"high") is how much of the read is backed by real data — a
    service-computed measure of how many sections' figures resolved, not a model
    guess. ``thesis`` is a one-line headline. ``sections`` grade the individual facets
    — profitability, cash generation, growth, valuation, financial health, earnings,
    and the analyst view — each with its own stance, label, plain-language summary,
    and supporting figures. ``disclaimer`` is a fixed reminder that this is
    informational, not financial advice — authored by the service, not the model.
    ``model`` and ``generated_at`` record what produced the read and when. Reasoned
    only over the figures the other stock endpoints expose; descriptive, not advice."""

    symbol: str
    recommendation: str  # "strong_buy" | "buy" | "hold" | "sell" | "strong_sell"
    confidence: str  # "low" | "medium" | "high"
    thesis: str
    sections: list[ScorecardSectionResponse]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class EarningsAnalysisResponse(BaseModel):
    """An AI-generated, plain-language read of a stock's earnings story.

    ``summary`` is the plain-language headline of how earnings have gone and where
    they look headed; ``trend`` is the direction ("accelerating"/"steady"/
    "slowing"); ``highlights`` are a few short takeaways. ``disclaimer`` is a fixed
    reminder that this is informational, not financial advice — authored by the
    service, not the model. ``model`` and ``generated_at`` record what produced the
    analysis and when. Reasoned only over the recent earnings timelines;
    descriptive, not advice."""

    symbol: str
    summary: str
    trend: str  # "accelerating" | "steady" | "slowing"
    highlights: list[str]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class RatingsAnalysisResponse(BaseModel):
    """An AI-generated, plain-language read of a stock's analyst coverage.

    ``verdict`` is the overall read ("bullish"/"mixed"/"cautious") and ``confidence`` how
    firmly it's held ("low"/"medium"/"high"); ``summary`` is the plain-language headline and
    ``findings`` a few short, concrete takeaways. ``disclaimer`` is a fixed reminder that this
    is informational, not financial advice — authored by the service, not the model. ``model``
    and ``generated_at`` record what produced the analysis and when. Reasoned only over the
    analyst coverage the card exposes; descriptive, not advice."""

    symbol: str
    verdict: str  # "bullish" | "mixed" | "cautious"
    confidence: str  # "low" | "medium" | "high"
    summary: str
    findings: list[str]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class FundamentalsAnalysisResponse(BaseModel):
    """An AI-generated, plain-language read of a stock's fundamentals.

    ``verdict`` is the overall read ("strong"/"mixed"/"weak") of the company's fundamentals —
    profitability, growth, balance-sheet health, and whether the shares look reasonably priced
    against all that — and ``confidence`` how firmly it's held ("low"/"medium"/"high");
    ``summary`` is the plain-language headline and ``findings`` a few short, concrete takeaways.
    ``disclaimer`` is a fixed reminder that this is informational, not financial advice — authored
    by the service, not the model. ``model`` and ``generated_at`` record what produced the
    analysis and when. Reasoned only over the fundamentals the ticker card exposes plus the
    industry-P/E peer benchmark; descriptive, not advice."""

    symbol: str
    verdict: str  # "strong" | "mixed" | "weak"
    confidence: str  # "low" | "medium" | "high"
    summary: str
    findings: list[str]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class SectorHighlightResponse(BaseModel):
    """One standout sector in a market analysis, with the AI's plain note.

    `symbol` is the proxy ETF the sector is read through; `change_percent` is that
    proxy's real move on the day (joined from the board, not authored by the model),
    and `note` is the model's one-line read on why it stands out."""

    sector: str
    symbol: str
    change_percent: float | None = None
    note: str


class SectorAnalysisResponse(BaseModel):
    """An AI-generated read of how the market's sectors are moving today.

    `summary` is the plain-language headline of which corners of the market are
    leading and lagging; `tone` is the risk posture the day's rotation implies
    ("risk_on"/"risk_off"/"mixed"); `leaders` and `laggards` are the standout
    sectors with a short note each. `disclaimer` is a fixed reminder that this is
    informational, not financial advice — authored by the service, not the model.
    `model` and `generated_at` record what produced the analysis and when. Reasoned
    only over the day's sector board; descriptive, not advice."""

    summary: str
    tone: str  # "risk_on" | "risk_off" | "mixed"
    leaders: list[SectorHighlightResponse]
    laggards: list[SectorHighlightResponse]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class MarketIndexReturnResponse(BaseModel):
    """One headline index's return over a single timeframe.

    `symbol` is the proxy ETF the index is read through (SPY for the S&P 500, QQQ
    for the Nasdaq); `change_percent` is that proxy's real percent move over the
    period (joined from the board, not authored by the model)."""

    name: str
    symbol: str
    change_percent: float | None = None


class MarketPeriodResponse(BaseModel):
    """One timeframe in the market summary — the past week, month, or year.

    `period` is "week"/"month"/"year"; `indexes` carries each index's real return
    over the window; `note` is the AI's one-line, plain-language read of the
    stretch."""

    period: str  # "week" | "month" | "year"
    indexes: list[MarketIndexReturnResponse]
    note: str


class MarketSummaryResponse(BaseModel):
    """An AI-generated overview of how the US market has moved lately.

    `summary` is the plain-language headline; `tone` is the risk posture the
    recent moves imply ("risk_on"/"risk_off"/"mixed"); `periods` breaks the read
    down by timeframe (the past year, month and week), each with the indexes' real
    returns and a one-line note. `disclaimer` is a fixed reminder that this is
    informational, not financial advice — authored by the service, not the model.
    `model` and `generated_at` record what produced the summary and when. Reasoned
    only over the day's index board; descriptive, not advice."""

    summary: str
    tone: str  # "risk_on" | "risk_off" | "mixed"
    periods: list[MarketPeriodResponse]
    disclaimer: str
    model: str  # the model that produced the summary
    generated_at: datetime
