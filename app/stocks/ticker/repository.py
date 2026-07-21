from abc import ABC, abstractmethod
from typing import NamedTuple


class StoredTickerFacts(NamedTuple):
    name: str | None = None
    exchange: str | None = None
    market_cap: float | None = None
    sector: str | None = None
    industry: str | None = None
    revenue_growth_yoy: float | None = None
    eps_growth_yoy: float | None = None
    forward_revenue_growth_yoy: float | None = None
    forward_eps_growth_yoy: float | None = None
    fcf_per_share: float | None = None
    ocf_per_share: float | None = None
    fcf_growth_yoy: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    return_on_equity: float | None = None
    current_ratio: float | None = None
    debt_to_equity: float | None = None
    beta: float | None = None
    book_value_per_share: float | None = None
    sales_per_share: float | None = None
    dividend_per_share: float | None = None
    ebitda: float | None = None
    total_debt: float | None = None
    cash_and_equivalents: float | None = None
    shares_outstanding: float | None = None


class TickerRepository(ABC):
    @abstractmethod
    def get_facts(self, symbol: str) -> StoredTickerFacts:
        raise NotImplementedError

    @abstractmethod
    def save_name(self, symbol: str, name: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def save_exchange(self, symbol: str, exchange: str) -> None:
        raise NotImplementedError
