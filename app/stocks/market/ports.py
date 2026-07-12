"""Application ports: the abstractions the market use cases depend on.

Both boards are read through proxy ETFs on the price feed, so the Alpaca
adapter implements these alongside the other price-derived ports.
"""

from abc import ABC, abstractmethod

from app.stocks.market.entities import MarketIndexPerformance, SectorPerformance


class SectorPerformanceProvider(ABC):
    """A gateway for each market sector's performance on the day.

    Sectors are read through their proxy ETFs rather than a dedicated sector
    feed, so this sits alongside the other price-derived ports.
    """

    @abstractmethod
    def get_sector_performance(self) -> list[SectorPerformance]:
        """Return the day's performance for every covered market sector.

        Raises:
            StockNotFound: no sector data is available at all.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class MarketOverviewProvider(ABC):
    """A gateway for the headline US indices' performance on the day.

    Like ``SectorPerformanceProvider``, the indices aren't directly tradable, so
    each is read through its proxy ETF (SPY -> S&P 500, QQQ -> Nasdaq); this sits
    alongside the other price-derived ports.
    """

    @abstractmethod
    def get_market_overview(self) -> list[MarketIndexPerformance]:
        """Return the day's performance for each headline US index.

        Raises:
            StockNotFound: no index data is available at all.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError
