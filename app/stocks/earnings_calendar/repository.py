from abc import ABC, abstractmethod
from datetime import date

from app.stocks.earnings_calendar.entities import EarningsCalendarItem


class EarningsCalendarRepository(ABC):
    @abstractmethod
    def upcoming(
        self, from_date: date, to_date: date, limit: int
    ) -> list[EarningsCalendarItem]:
        raise NotImplementedError
