from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from app.domains.shared.entities import StockPerformance


class HeatMapScope(str, Enum):
    SP500 = "sp500"
    NASDAQ100 = "nasdaq100"


@dataclass(frozen=True)
class HeatMapRow:
    ticker: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap: float


@dataclass(frozen=True)
class HeatMapCell:
    ticker: str
    name: str | None
    market_cap: float
    change_percent: float | None
    performance: StockPerformance | None


@dataclass(frozen=True)
class HeatMapIndustry:
    industry: str | None
    market_cap: float
    cells: tuple[HeatMapCell, ...]


@dataclass(frozen=True)
class HeatMapSector:
    sector: str
    market_cap: float
    industries: tuple[HeatMapIndustry, ...]


@dataclass(frozen=True)
class HeatMap:
    scope: HeatMapScope
    sectors: tuple[HeatMapSector, ...]

    @property
    def cell_count(self) -> int:
        return sum(
            len(industry.cells)
            for sector in self.sectors
            for industry in sector.industries
        )

    @classmethod
    def build(
        cls,
        scope: HeatMapScope,
        rows: tuple[HeatMapRow, ...],
        change_by_ticker: Mapping[str, float | None],
        performance_by_ticker: Mapping[str, StockPerformance] | None = None,
    ) -> "HeatMap":
        performance_by_ticker = performance_by_ticker or {}
        grouped: dict[str, dict[str | None, list[HeatMapCell]]] = {}
        for row in rows:
            if row.sector is None:
                continue
            cell = HeatMapCell(
                ticker=row.ticker,
                name=row.name,
                market_cap=row.market_cap,
                change_percent=change_by_ticker.get(row.ticker),
                performance=performance_by_ticker.get(row.ticker),
            )
            grouped.setdefault(row.sector, {}).setdefault(row.industry, []).append(cell)

        sectors: list[HeatMapSector] = []
        for sector, industries in grouped.items():
            industry_groups: list[HeatMapIndustry] = []
            for industry, cells in industries.items():
                ordered = tuple(
                    sorted(cells, key=lambda c: (-c.market_cap, c.ticker))
                )
                industry_groups.append(
                    HeatMapIndustry(
                        industry=industry,
                        market_cap=sum(c.market_cap for c in ordered),
                        cells=ordered,
                    )
                )
            # Nulls (unclassified industry) sort last within the sector via the "" fallback.
            industry_groups.sort(key=lambda g: (-g.market_cap, g.industry or ""))
            sectors.append(
                HeatMapSector(
                    sector=sector,
                    market_cap=sum(g.market_cap for g in industry_groups),
                    industries=tuple(industry_groups),
                )
            )
        sectors.sort(key=lambda s: (-s.market_cap, s.sector))
        return cls(scope=scope, sectors=tuple(sectors))
