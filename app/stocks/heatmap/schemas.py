"""HTTP response DTOs for the heat-map endpoint (``GET /market/heatmap``).

Pydantic models at the slice edge, kept separate from the entities so the domain stays
framework-free. The shape is the nested tree a treemap renders directly — ``sectors`` →
``industries`` → ``stocks`` — each node carrying its ``market_cap`` (the tile size) and each
leaf its ``change_percent`` (the day tile's colour, ``null`` when the live feed had no quote)
plus its ``performance`` (the trailing-window returns behind the timeframe selector).
"""

from __future__ import annotations

from pydantic import BaseModel

from app.stocks.schemas import StockPerformanceResponse


class HeatMapStockResponse(BaseModel):
    """One stock tile: sized by ``market_cap``, coloured by ``change_percent`` (``null`` =
    uncoloured, no live quote). ``performance`` carries the trailing-window returns (1W…1Y, YTD)
    the board colours by for a non-day timeframe — ``null`` when no history was fetched."""

    ticker: str
    name: str | None
    market_cap: float
    change_percent: float | None
    performance: StockPerformanceResponse | None = None


class HeatMapIndustryResponse(BaseModel):
    """An industry group within a sector — its stocks and their combined ``market_cap``.
    ``industry`` is ``null`` for a sector's not-yet-classified stocks."""

    industry: str | None
    market_cap: float
    stocks: list[HeatMapStockResponse]


class HeatMapSectorResponse(BaseModel):
    """A sector group — its industry sub-groups and their combined ``market_cap``."""

    sector: str
    market_cap: float
    industries: list[HeatMapIndustryResponse]


class HeatMapResponse(BaseModel):
    """The whole board: which index it covers, the tile count, and the sector tree
    (largest sector first)."""

    scope: str
    count: int
    sectors: list[HeatMapSectorResponse]
