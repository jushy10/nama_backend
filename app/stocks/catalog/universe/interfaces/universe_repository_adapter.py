from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from app.stocks.catalog.universe.entities import CompanyClassification, ScreenedStock
from app.stocks.catalog.universe.interfaces.types import UniverseSyncCounts


class UniverseRepositoryAdapter(ABC):
    @abstractmethod
    def upsert_screen(self, stocks: tuple[ScreenedStock, ...]) -> UniverseSyncCounts:
        raise NotImplementedError

    @abstractmethod
    def us_domiciled_company_names(self) -> frozenset[str]:
        raise NotImplementedError

    @abstractmethod
    def delete_stocks(self, tickers: Sequence[str]) -> int:
        raise NotImplementedError

    @abstractmethod
    def tickers_missing_classification(self, limit: int) -> tuple[str, ...]:
        raise NotImplementedError

    @abstractmethod
    def set_classification(
        self, ticker: str, classification: CompanyClassification
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_pe_ratios(self, pe_by_ticker: Mapping[str, float | None]) -> int:
        raise NotImplementedError

    @abstractmethod
    def fcf_per_share_by_ticker(self) -> Mapping[str, float]:
        raise NotImplementedError

    @abstractmethod
    def set_fcf_yields(self, fcf_yield_by_ticker: Mapping[str, float | None]) -> int:
        raise NotImplementedError

    @abstractmethod
    def ev_components_by_ticker(self) -> Mapping[str, tuple[float, float | None, float | None]]:
        raise NotImplementedError

    @abstractmethod
    def set_ev_ebitda(self, ev_ebitda_by_ticker: Mapping[str, float | None]) -> int:
        raise NotImplementedError
