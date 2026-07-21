from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.stocks.analysis.entities import InvestmentAnalysis
from app.stocks.etfs.entities import (
    EtfDetail,
    EtfProfile,
    EtfScreenIntent,
    ScreenedEtf,
)


class EtfScreener(ABC):
    @abstractmethod
    def screen(self, *, min_net_assets: float) -> tuple[ScreenedEtf, ...]:
        raise NotImplementedError


class EtfProfileProvider(ABC):
    @abstractmethod
    def get_profile(self, symbol: str) -> EtfProfile:
        raise NotImplementedError


class EtfAnalysisProvider(ABC):
    @abstractmethod
    def analyze(self, detail: EtfDetail) -> InvestmentAnalysis:
        raise NotImplementedError


class EtfScreenerQueryTranslator(ABC):
    @abstractmethod
    def translate(
        self,
        query: str,
        *,
        categories: Sequence[str],
    ) -> EtfScreenIntent:
        raise NotImplementedError
