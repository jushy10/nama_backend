from datetime import date

from pydantic import BaseModel


class InsiderTransactionResponse(BaseModel):
    filing_date: date
    transaction_date: date | None
    insider_name: str
    role: str
    security_title: str | None
    transaction_code: str
    code_label: str
    acquired_disposed: str | None
    is_open_market: bool
    is_open_market_buy: bool
    is_open_market_sale: bool
    shares: float | None
    price_per_share: float | None
    value: float | None
    shares_owned_following: float | None


class InsiderSummaryResponse(BaseModel):
    open_market_buy_count: int
    open_market_sell_count: int
    open_market_buy_value: float
    open_market_sell_value: float
    net_value: float


class InsiderActivityResponse(BaseModel):
    symbol: str
    count: int
    summary: InsiderSummaryResponse
    transactions: list[InsiderTransactionResponse]
