from __future__ import annotations

from pydantic import BaseModel

from app.domains.shared.schemas import StockPerformanceResponse


class HeatMapStockResponse(BaseModel):
    ticker: str
    name: str | None
    market_cap: float
    change_percent: float | None
    performance: StockPerformanceResponse | None = None


class HeatMapIndustryResponse(BaseModel):
    industry: str | None
    market_cap: float
    stocks: list[HeatMapStockResponse]


class HeatMapSectorResponse(BaseModel):
    sector: str
    market_cap: float
    industries: list[HeatMapIndustryResponse]


class HeatMapResponse(BaseModel):
    scope: str
    count: int
    sectors: list[HeatMapSectorResponse]
