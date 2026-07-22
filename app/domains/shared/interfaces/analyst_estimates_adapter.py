from abc import ABC, abstractmethod
from app.domains.shared.entities import AnalystEstimates


class AnalystEstimatesAdapter(ABC):
    @abstractmethod
    def get_estimates(self, symbol: str) -> AnalystEstimates:
        raise NotImplementedError
