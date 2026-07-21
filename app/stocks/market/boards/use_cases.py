from app.stocks.market.boards.entities import MarketIndexPerformance, SectorPerformance
from app.stocks.market.boards.ports import MarketOverviewProvider, SectorPerformanceProvider


class GetSectorPerformance:
    def __init__(self, provider: SectorPerformanceProvider) -> None:
        self._provider = provider

    def execute(self) -> list[SectorPerformance]:
        sectors = self._provider.get_sector_performance()
        # Best performer first; a None percent (no quote) sorts to the end.
        return sorted(
            sectors,
            key=lambda s: (s.change_percent is None, -(s.change_percent or 0.0)),
        )


class GetMarketOverview:
    def __init__(self, provider: MarketOverviewProvider) -> None:
        self._provider = provider

    def execute(self) -> list[MarketIndexPerformance]:
        return self._provider.get_market_overview()
