from abc import ABC, abstractmethod
from datetime import date
from app.stocks.seo.interfaces.types import CongressPageTrade, EtfPageFacts, SectorStock, StockPageRef, TickerPageFacts


class SeoReadRepositoryAdapter(ABC):
    @abstractmethod
    def get_ticker_facts(self, ticker: str) -> TickerPageFacts | None:
        raise NotImplementedError

    @abstractmethod
    def list_stock_pages(self, limit: int) -> tuple[StockPageRef, ...]:
        raise NotImplementedError

    @abstractmethod
    def list_sector_stocks(self, sector: str, limit: int) -> tuple[SectorStock, ...]:
        raise NotImplementedError

    @abstractmethod
    def list_sectors(self) -> tuple[str, ...]:
        raise NotImplementedError

    @abstractmethod
    def list_screen_stocks(
        self, sort_key: str, *, descending: bool, positive_only: bool, limit: int
    ) -> tuple[SectorStock, ...]:
        raise NotImplementedError

    @abstractmethod
    def get_etf_facts(self, ticker: str) -> EtfPageFacts | None:
        raise NotImplementedError

    @abstractmethod
    def list_etf_pages(self, limit: int) -> tuple[StockPageRef, ...]:
        raise NotImplementedError

    @abstractmethod
    def list_brief_dates(self, limit: int) -> tuple[date, ...]:
        raise NotImplementedError

    @abstractmethod
    def list_recent_congress_trades(self, limit: int) -> tuple[CongressPageTrade, ...]:
        raise NotImplementedError

    @abstractmethod
    def list_congress_trades_for_ticker(
        self, ticker: str, limit: int
    ) -> tuple[CongressPageTrade, ...]:
        raise NotImplementedError
