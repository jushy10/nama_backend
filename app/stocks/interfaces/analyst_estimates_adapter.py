from abc import ABC, abstractmethod
from app.stocks.entities import AnalystEstimates


class AnalystEstimatesAdapter(ABC):
    @abstractmethod
    def get_estimates(self, symbol: str) -> AnalystEstimates:
        raise NotImplementedError
