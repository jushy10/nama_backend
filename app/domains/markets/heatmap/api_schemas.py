from __future__ import annotations

from pydantic import BaseModel

from app.domains.markets.heatmap.entities import (
    HeatMap,
    HeatMapCell,
    HeatMapIndustry,
    HeatMapSector,
)
from app.domains.shared.schemas import StockPerformanceResponse


class HeatMapStockResponse(BaseModel):
    ticker: str
    name: str | None
    market_cap: float
    change_percent: float | None
    performance: StockPerformanceResponse | None = None

    @classmethod
    def from_cell(cls, cell: HeatMapCell) -> "HeatMapStockResponse":
        return cls(
            ticker=cell.ticker,
            name=cell.name,
            market_cap=cell.market_cap,
            change_percent=cell.change_percent,
            performance=StockPerformanceResponse.from_performance(cell.performance),
        )


class HeatMapIndustryResponse(BaseModel):
    industry: str | None
    market_cap: float
    stocks: list[HeatMapStockResponse]

    @classmethod
    def from_industry(cls, industry: HeatMapIndustry) -> "HeatMapIndustryResponse":
        return cls(
            industry=industry.industry,
            market_cap=industry.market_cap,
            stocks=[HeatMapStockResponse.from_cell(c) for c in industry.cells],
        )


class HeatMapSectorResponse(BaseModel):
    sector: str
    market_cap: float
    industries: list[HeatMapIndustryResponse]

    @classmethod
    def from_sector(cls, sector: HeatMapSector) -> "HeatMapSectorResponse":
        return cls(
            sector=sector.sector,
            market_cap=sector.market_cap,
            industries=[
                HeatMapIndustryResponse.from_industry(i) for i in sector.industries
            ],
        )


class HeatMapResponse(BaseModel):
    scope: str
    count: int
    sectors: list[HeatMapSectorResponse]

    @classmethod
    def from_heat_map(cls, heatmap: HeatMap) -> "HeatMapResponse":
        return cls(
            scope=heatmap.scope.value,
            count=heatmap.cell_count,
            sectors=[HeatMapSectorResponse.from_sector(s) for s in heatmap.sectors],
        )
