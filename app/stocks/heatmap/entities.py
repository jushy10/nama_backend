"""Domain entities for the heat-map slice — the market treemap (sector → industry → stock).

A Finviz-style heat map: every stock is a tile *sized* by its market cap and *coloured* by the
day's price change, grouped into its sector and, within that, its industry. These entities model
that nested shape and the rules that build it — the grouping, the cap sums that size each tile,
and the largest-first ordering are all *facts about the map*, so they live here (in
:meth:`HeatMap.build`), not in the use case.

Vendor-agnostic and framework-free: the module imports only stdlib plus the shared stocks-core
``StockPerformance`` value object (the same trailing-window shape the sector board and ticker card
reuse — not another *slice's* shape). The use case feeds it plain ``HeatMapRow`` inputs (mapped
from the universe read), a change-by-ticker map (mapped from the live quotes) and a
performance-by-ticker map (mapped from the batched daily bars), so the entity never depends on a
data vendor or the web framework.

A tile's *size* (market cap) always exists — a screened row always carries one. Its *colour*
(``change_percent``) is ``None`` when the live feed had no quote for the symbol (e.g. a name the
free IEX feed doesn't carry, or a feed hiccup): the tile still shows, sized but uncoloured. Its
trailing windows (``performance``) are likewise ``None`` when no daily-bar history was fetched for
the symbol — the day-move tile still renders, just without the longer-timeframe colours.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from app.stocks.entities import StockPerformance


class HeatMapScope(str, Enum):
    """Which slice of the universe the map covers — the ``?index=`` selector.

    The two headline US indices, read off the ``in_sp500`` / ``in_nasdaq100`` flags the
    index-membership sync reconciles onto the ``stocks`` anchor. Bounded sets (~500 / ~100
    names) with near-complete sector coverage — the canonical heat-map universes, and small
    enough that one board of live quotes is a handful of batched snapshot calls.
    """

    SP500 = "sp500"
    NASDAQ100 = "nasdaq100"


@dataclass(frozen=True)
class HeatMapRow:
    """One screened stock as the map's input: identity + grouping keys + size.

    The entity's own input shape (not another slice's), so the domain stays self-contained: the
    use case maps each universe search result onto one of these. ``sector`` may be ``None`` (a
    stock not yet classified) — such a row can't be placed in the treemap and is dropped by
    :meth:`HeatMap.build`; ``industry`` may be ``None`` and forms its own bucket within a sector.
    """

    ticker: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap: float


@dataclass(frozen=True)
class HeatMapCell:
    """One stock's tile: sized by ``market_cap``, coloured by ``change_percent`` (or, for a
    non-day timeframe, by one of ``performance``'s trailing windows).

    ``change_percent`` is the day's move (percent), ``None`` when the live feed carried no quote
    for the symbol — the tile still renders, sized but uncoloured. ``performance`` carries the
    trailing-window returns (1W…1Y, YTD) that back the board's timeframe selector, ``None`` when
    no daily-bar history was fetched for the symbol (the day-move tile still renders).
    """

    ticker: str
    name: str | None
    market_cap: float
    change_percent: float | None
    performance: StockPerformance | None


@dataclass(frozen=True)
class HeatMapIndustry:
    """An industry group within a sector — a sub-treemap of its stocks (largest tile first).

    ``market_cap`` is the sum of the group's cells (the group tile's size). ``industry`` is the
    stored slug, or ``None`` for a sector's not-yet-classified stocks (their own bucket).
    """

    industry: str | None
    market_cap: float
    cells: tuple[HeatMapCell, ...]


@dataclass(frozen=True)
class HeatMapSector:
    """A sector group — the top-level treemap tile, holding its industry sub-groups.

    ``market_cap`` is the sum over its industries (so sectors are sized and ordered by total
    weight, the biggest sector first — exactly how the board reads left to right / top down).
    """

    sector: str
    market_cap: float
    industries: tuple[HeatMapIndustry, ...]


@dataclass(frozen=True)
class HeatMap:
    """The whole map: sectors (largest first), each an industry → stock sub-tree.

    ``scope`` records which universe it covers. Built from flat rows + a change-by-ticker map via
    :meth:`build`, which does all the grouping, cap-summing and ordering — every rule here is a
    fact about the map, so a caller just supplies the ingredients.
    """

    scope: HeatMapScope
    sectors: tuple[HeatMapSector, ...]

    @property
    def cell_count(self) -> int:
        """Total tiles across every sector/industry — the board's stock count."""
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
        """Fold flat rows + their day-change (+ trailing performance) into the nested, sized,
        ordered treemap.

        Groups sector → industry → cell (a row with no ``sector`` is dropped — there's nowhere
        to place it), attaching each cell's ``change_percent`` from ``change_by_ticker`` (absent
        → ``None``, an uncoloured tile) and its trailing windows from ``performance_by_ticker``
        (absent → ``None``, blank timeframe colours; omit the map entirely for a day-move-only
        board). Every group's ``market_cap`` is the sum of its members, and every level is ordered
        **largest cap first** with a stable name tiebreak — so the map is deterministic (tests) and
        draws big tiles first (the reader's eye). Sectors with no placeable rows simply don't
        appear; an empty input yields a map with no sectors.
        """
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
