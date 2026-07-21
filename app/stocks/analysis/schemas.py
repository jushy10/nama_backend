from datetime import datetime

from pydantic import BaseModel


class SectionMetricResponse(BaseModel):
    label: str
    value: str


class ScorecardSectionResponse(BaseModel):
    key: str
    title: str
    stance: str  # "positive" | "neutral" | "negative"
    label: str
    summary: str
    metrics: list[SectionMetricResponse]


class InvestmentAnalysisResponse(BaseModel):
    symbol: str
    recommendation: str  # "strong_buy" | "buy" | "hold" | "sell" | "strong_sell"
    confidence: str  # "low" | "medium" | "high"
    thesis: str
    sections: list[ScorecardSectionResponse]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class EarningsAnalysisResponse(BaseModel):
    symbol: str
    summary: str
    trend: str  # "accelerating" | "steady" | "slowing"
    highlights: list[str]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class RatingsAnalysisResponse(BaseModel):
    symbol: str
    verdict: str  # "bullish" | "mixed" | "cautious"
    confidence: str  # "low" | "medium" | "high"
    summary: str
    findings: list[str]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class FundamentalsAnalysisResponse(BaseModel):
    symbol: str
    verdict: str  # "strong" | "mixed" | "weak"
    confidence: str  # "low" | "medium" | "high"
    summary: str
    findings: list[str]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class SectorMoverResponse(BaseModel):
    ticker: str
    name: str | None = None
    change_percent: float | None = None
    market_cap: float | None = None


class SectorHeadlineResponse(BaseModel):
    ticker: str
    title: str
    published_at: datetime | None = None
    publisher: str | None = None
    link: str | None = None


class SectorHighlightResponse(BaseModel):
    sector: str
    symbol: str
    change_percent: float | None = None
    note: str
    movers: list[SectorMoverResponse] = []
    headlines: list[SectorHeadlineResponse] = []


class SectorAnalysisResponse(BaseModel):
    summary: str
    tone: str  # "risk_on" | "risk_off" | "mixed"
    leaders: list[SectorHighlightResponse]
    laggards: list[SectorHighlightResponse]
    disclaimer: str
    model: str  # the model that produced the analysis
    generated_at: datetime


class MarketIndexReturnResponse(BaseModel):
    name: str
    symbol: str
    change_percent: float | None = None


class MarketPeriodResponse(BaseModel):
    period: str  # "week" | "month" | "year"
    indexes: list[MarketIndexReturnResponse]
    note: str


class MarketSummaryResponse(BaseModel):
    summary: str
    tone: str  # "risk_on" | "risk_off" | "mixed"
    periods: list[MarketPeriodResponse]
    disclaimer: str
    model: str  # the model that produced the summary
    generated_at: datetime
