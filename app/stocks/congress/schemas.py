from datetime import date

from pydantic import BaseModel


class CongressTradeResponse(BaseModel):
    member: str
    chamber: str
    party: str | None
    ticker: str
    name: str | None
    tx_type: str
    amount_range: str | None
    amount_midpoint: float | None
    transaction_date: date | None
    disclosure_date: date | None
    owner: str | None
    source_url: str | None
    is_buy: bool
    is_sell: bool


class CongressSummaryResponse(BaseModel):
    buy_count: int
    sell_count: int
    buy_value: float
    sell_value: float
    net_value: float


class CongressActivityResponse(BaseModel):
    symbol: str
    total: int
    limit: int
    offset: int
    count: int
    summary: CongressSummaryResponse
    items: list[CongressTradeResponse]


class CongressMarketActivityResponse(BaseModel):
    window: str
    total: int
    limit: int
    offset: int
    count: int
    summary: CongressSummaryResponse
    items: list[CongressTradeResponse]


class CongressLeaderboardEntryResponse(BaseModel):
    ticker: str
    name: str | None
    trade_count: int
    member_count: int
    buy_count: int
    sell_count: int
    buy_value: float
    sell_value: float
    net_value: float
    total_value: float
    last_activity: date | None


class CongressLeaderboardResponse(BaseModel):
    window: str
    metric: str
    total: int
    count: int
    items: list[CongressLeaderboardEntryResponse]
