from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class TickerPageFacts:
    name: str | None
    exchange: str | None
    sector: str | None
    industry: str | None
    market_cap: float | None
    pe_ratio: float | None
    fcf_yield: float | None
    revenue_growth_yoy: float | None
    eps_growth_yoy: float | None
    fcf_growth_yoy: float | None
    in_sp500: bool
    in_nasdaq100: bool


@dataclass(frozen=True)
class StockPageRef:
    ticker: str
    last_modified: date | None


@dataclass(frozen=True)
class SectorStock:
    ticker: str
    name: str | None
    market_cap: float | None
    pe_ratio: float | None
    fcf_yield: float | None


@dataclass(frozen=True)
class CongressPageTrade:
    ticker: str
    name: str | None
    member: str
    chamber: str
    tx_type: str
    amount_range: str | None
    transaction_date: date | None
    disclosure_date: date | None


@dataclass(frozen=True)
class EtfPageFacts:
    name: str | None
    exchange: str | None
    category: str | None
    net_assets: float | None
    expense_ratio: float | None
    fund_family: str | None
    dividend_yield: float | None
    nav: float | None
    description: str | None
