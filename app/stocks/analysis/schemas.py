"""HTTP response models for the AI-analysis endpoints.

Pydantic is a web/serialization detail, so these DTOs live at the edge —
deliberately separate from the entities so the core stays framework-agnostic.
"""

from datetime import datetime

from pydantic import BaseModel


class InvestmentAnalysisResponse(BaseModel):
    """An AI-generated, balanced buy/hold/sell read on a stock.

    ``recommendation`` is the headline call ("buy"/"hold"/"sell") and
    ``confidence`` how firmly it's held ("low"/"medium"/"high"); ``thesis`` is a
    few sentences of reasoning, with ``strengths`` (the bull case) and ``risks``
    (the bear case) as short bullets. ``disclaimer`` is a fixed reminder that this
    is informational, not financial advice — authored by the service, not the
    model. ``model`` and ``generated_at`` record what produced the analysis and
    when. Reasoned only over the figures the other stock endpoints expose;
    descriptive, not advice."""

    symbol: str
    recommendation: str  # "buy" | "hold" | "sell"
    confidence: str  # "low" | "medium" | "high"
    thesis: str
    strengths: list[str]  # bull-case points
    risks: list[str]  # bear-case points
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
