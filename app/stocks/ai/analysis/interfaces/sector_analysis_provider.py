from abc import ABC, abstractmethod

from app.stocks.ai.analysis.entities import SectorAnalysis, SectorContext


class SectorAnalysisProvider(ABC):
    @abstractmethod
    def analyze(self, contexts: list[SectorContext]) -> SectorAnalysis:
        raise NotImplementedError
