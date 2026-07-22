from abc import ABC, abstractmethod

from app.domains.research.analysis.entities import SectorAnalysis, SectorContext


class SectorAnalysisAdapter(ABC):
    @abstractmethod
    def analyze(self, contexts: list[SectorContext]) -> SectorAnalysis:
        raise NotImplementedError
