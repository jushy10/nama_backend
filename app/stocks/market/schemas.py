from datetime import datetime

from pydantic import BaseModel

from app.stocks.schemas import StockPerformanceResponse


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


class SectorBoardResponse(BaseModel):
    count: int
    sectors: list[SectorPerformanceResponse]
