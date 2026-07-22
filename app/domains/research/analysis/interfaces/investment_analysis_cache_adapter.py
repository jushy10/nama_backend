from abc import ABC, abstractmethod

from app.domains.research.analysis.entities import InvestmentAnalysis


class InvestmentAnalysisCacheAdapter(ABC):
    @abstractmethod
    def get(self, symbol: str) -> InvestmentAnalysis | None:
        raise NotImplementedError

    @abstractmethod
    def put(self, analysis: InvestmentAnalysis) -> None:
        raise NotImplementedError
