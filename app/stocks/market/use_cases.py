"""Application Business Rules: the market-board use cases.

The non-AI, whole-market reads: the ranked sector board and the headline index
board. The AI reads over these boards (``GetSectorAnalysis`` /
``GetMarketSummary``) live in the analysis slice and reuse these use cases.
"""

from app.stocks.market.entities import MarketIndexPerformance, SectorPerformance
from app.stocks.market.ports import MarketOverviewProvider, SectorPerformanceProvider


class GetSectorPerformance:
    """Use case: rank the market's sectors by their move on the day.

    Takes no input — it reports on the whole market. Sectors come back best
    performer first; any sector missing a quote (so no percent move) sorts last.
    """

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
    """Use case: the headline US indices' performance (the S&P 500 and Nasdaq).

    Takes no input — it reports on the whole market. Returns the indices in the
    provider's stable order (broad market first), each carrying its day move and
    trailing-window returns.
    """

    def __init__(self, provider: MarketOverviewProvider) -> None:
        self._provider = provider

    def execute(self) -> list[MarketIndexPerformance]:
        return self._provider.get_market_overview()
