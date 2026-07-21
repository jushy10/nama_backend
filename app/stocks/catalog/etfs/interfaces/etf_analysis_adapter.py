from abc import ABC, abstractmethod
from app.stocks.ai.analysis.entities import InvestmentAnalysis
from app.stocks.catalog.etfs.entities import EtfDetail


class EtfAnalysisAdapter(ABC):
    @abstractmethod
    def analyze(self, detail: EtfDetail) -> InvestmentAnalysis:
        raise NotImplementedError
