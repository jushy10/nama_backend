"""HTTP response models for the market-board endpoints.

Pydantic is a web/serialization detail, so these DTOs live at the edge —
deliberately separate from the entities so the core stays framework-agnostic.
"""

from datetime import datetime

from pydantic import BaseModel

from app.stocks.schemas import StockPerformanceResponse


class SectorPerformanceResponse(BaseModel):
    """One market sector's move on the day.

    `symbol` is the proxy ETF the sector is read through (e.g. XLK for
    Technology); `change_percent` is that proxy's percent move on the day."""

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
    """The day's full set of sectors, ranked best performer first."""

    count: int
    sectors: list[SectorPerformanceResponse]
