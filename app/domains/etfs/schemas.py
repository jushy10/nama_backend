from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.domains.shared.schemas import StockPerformanceResponse


class EtfSearchItemResponse(BaseModel):
    ticker: str
    name: str | None = None
    exchange: str | None = None
    net_assets: float | None = None  # raw USD (AUM)
    expense_ratio: float | None = None  # percent
    category: str | None = None  # Yahoo fund-category slug
    dividend_yield: float | None = None  # percent (trailing distribution yield)


class EtfSearchResponse(BaseModel):
    total: int
    limit: int
    offset: int
    count: int
    results: list[EtfSearchItemResponse]


class EtfCategoriesResponse(BaseModel):
    categories: list[str]


class AiEtfScreenInterpretationResponse(BaseModel):
    query: str | None = None
    categories: list[str] = []
    sort: str | None = None
    direction: str = "desc"
    limit: int | None = None


class AiEtfScreenResponse(BaseModel):
    interpreted: AiEtfScreenInterpretationResponse


class EtfHoldingResponse(BaseModel):
    ticker: str | None = None
    name: str | None = None
    weight: float | None = None  # percent of fund


class EtfSectorWeightResponse(BaseModel):
    sector: str
    weight: float  # percent of fund


class EtfMetricsResponse(BaseModel):
    expense_ratio: float | None = None  # percent
    nav: float | None = None  # net asset value per share (raw price)
    net_assets: float | None = None  # AUM (raw)


class EtfDividendsResponse(BaseModel):
    yield_percentage: float | None = None  # percent


class EtfPerformanceResponse(StockPerformanceResponse):
    three_year: float | None = Field(default=None, alias="3y")  # percent (annualized avg, Yahoo)
    five_year: float | None = Field(default=None, alias="5y")  # percent (annualized avg, Yahoo)


class EtfDetailResponse(BaseModel):
    ticker: str
    name: str | None = None
    exchange: str | None = None
    asset_type: Literal["etf"] = "etf"  # always "etf" — this endpoint only serves funds
    # The live quote (Alpaca), primary.
    price: float
    change: float | None = None  # absolute move vs the previous close
    change_percent: float | None = None  # percent move vs the previous close
    previous_close: float | None = None
    as_of: datetime | None = None
    # Stored etfs-table facts.
    category: str | None = None  # fund-category slug (e.g. "large_blend")
    # Always-on best-effort Yahoo (yfinance) enrichment — null / [] when unavailable.
    fund_family: str | None = None
    description: str | None = None
    top_holdings: list[
        EtfHoldingResponse
    ] = []  # up to 10, largest first; [] if unavailable
    sector_weightings: list[
        EtfSectorWeightResponse
    ] = []  # weight desc; [] if unavailable
    # Opt-in blocks (?include=metrics,dividends,performance) — null unless requested.
    metrics: EtfMetricsResponse | None = None
    dividends: EtfDividendsResponse | None = None
    performance: EtfPerformanceResponse | None = None


class EtfAnalysisResponse(BaseModel):
    ticker: str
    asset_type: Literal["etf"] = "etf"
    recommendation: str  # "strong_buy" | "buy" | "hold" | "sell" | "strong_sell"
    confidence: str  # "low" | "medium" | "high"
    thesis: str
    strengths: list[str]  # bull-case points
    risks: list[str]  # bear-case points
    disclaimer: str  # authored by the service, not the model
    model: str
    generated_at: datetime
