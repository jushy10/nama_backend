from abc import ABC, abstractmethod
from app.stocks.catalog.universe.entities import AnchorMetrics, Classifications, MarketCapTier, PeerCompany, StockSearchCriteria, StockSearchPage


class StockSearchRepositoryAdapter(ABC):
    @abstractmethod
    def search(self, criteria: StockSearchCriteria) -> StockSearchPage:
        raise NotImplementedError

    @abstractmethod
    def classifications(self) -> Classifications:
        raise NotImplementedError

    @abstractmethod
    def pe_ratios_for_industry(self, industry: str) -> tuple[float, ...]:
        raise NotImplementedError

    @abstractmethod
    def industry_for_ticker(self, ticker: str) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def anchor_metrics_for_ticker(self, ticker: str) -> AnchorMetrics:
        raise NotImplementedError

    @abstractmethod
    def tier_for_ticker(self, ticker: str) -> MarketCapTier | None:
        raise NotImplementedError

    @abstractmethod
    def industry_peers(
        self, industry: str
    ) -> tuple[tuple[float, MarketCapTier], ...]:
        raise NotImplementedError

    @abstractmethod
    def peers_for_industry(self, industry: str) -> tuple[PeerCompany, ...]:
        raise NotImplementedError
