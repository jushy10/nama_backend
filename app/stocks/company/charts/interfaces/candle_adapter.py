from abc import ABC, abstractmethod
from datetime import datetime
from app.stocks.entities import CandleSeries, Timeframe


class CandleAdapter(ABC):
    @abstractmethod
    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> CandleSeries:
        raise NotImplementedError
