from datetime import datetime

from pydantic import BaseModel

from app.domains.markets.boards.entities import SectorPerformance
from app.domains.shared.schemas import StockPerformanceResponse


class SectorPerformanceResponse(BaseModel):
    sector: str
    symbol: str
    price: float
    change: float | None = None
    change_percent: float | None = None
    previous_close: float | None = None
    as_of: datetime | None = None
    # Trailing-window returns (percent), keyed 1w/1m/3m/6m/ytd/1y in JSON.
    performance: StockPerformanceResponse | None = None

    @classmethod
    def from_sector(cls, sector: SectorPerformance) -> "SectorPerformanceResponse":
        return cls(
            sector=sector.sector,
            symbol=sector.symbol,
            price=sector.price,
            change=sector.change,
            change_percent=sector.change_percent,
            previous_close=sector.previous_close,
            as_of=sector.as_of,
            performance=StockPerformanceResponse.from_performance(sector.performance),
        )


class SectorBoardResponse(BaseModel):
    count: int
    sectors: list[SectorPerformanceResponse]

    @classmethod
    def from_sectors(cls, sectors: list[SectorPerformance]) -> "SectorBoardResponse":
        return cls(
            count=len(sectors),
            sectors=[SectorPerformanceResponse.from_sector(s) for s in sectors],
        )
