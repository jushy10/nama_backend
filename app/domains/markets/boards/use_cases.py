from app.domains.markets.boards.entities import MarketIndexPerformance, SectorPerformance
from app.domains.markets.boards.interfaces import MarketOverviewAdapter, SectorPerformanceAdapter


class GetSectorPerformance:
    def __init__(self, provider: SectorPerformanceAdapter) -> None:
        self._provider = provider

    def execute(self) -> list[SectorPerformance]:
        sectors = self._provider.get_sector_performance()
        # Best performer first; a None percent (no quote) sorts to the end.
        return sorted(
            sectors,
            key=lambda s: (s.change_percent is None, -(s.change_percent or 0.0)),
        )


class GetMarketOverview:
    def __init__(self, provider: MarketOverviewAdapter) -> None:
        self._provider = provider

    def execute(self) -> list[MarketIndexPerformance]:
        return self._provider.get_market_overview()
