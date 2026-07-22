from abc import ABC, abstractmethod
from app.domains.research.analysis.entities import InvestmentAnalysis
from app.domains.etfs.entities import EtfDetail


class EtfAnalysisAdapter(ABC):
    @abstractmethod
    def analyze(self, detail: EtfDetail) -> InvestmentAnalysis:
        raise NotImplementedError
