from abc import ABC, abstractmethod

from app.stocks.ai.analysis.entities import InvestmentAnalysis


class InvestmentAnalysisCache(ABC):
    @abstractmethod
    def get(self, symbol: str) -> InvestmentAnalysis | None:
        raise NotImplementedError

    @abstractmethod
    def put(self, analysis: InvestmentAnalysis) -> None:
        raise NotImplementedError
