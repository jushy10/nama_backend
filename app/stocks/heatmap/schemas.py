"""HTTP response DTOs for the heat-map endpoint (``GET /market/heatmap``).

Pydantic models at the slice edge, kept separate from the entities so the domain stays
framework-free. The shape is the nested tree a treemap renders directly — ``sectors`` →
``industries`` → ``stocks`` — each node carrying its ``market_cap`` (the tile size) and each
leaf its ``change_percent`` (the tile colour, ``null`` when the live feed had no quote).
"""

from __future__ import annotations

from pydantic import BaseModel


class HeatMapStockResponse(BaseModel):
    """One stock tile: sized by ``market_cap``, coloured by ``change_percent`` (``null`` =
    uncoloured, no live quote)."""

    ticker: str
    name: str | None
    market_cap: float
    change_percent: float | None


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
