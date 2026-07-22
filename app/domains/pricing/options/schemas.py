from datetime import date

from pydantic import BaseModel


class OptionContractResponse(BaseModel):
    expiration: date
    strike: float
    type: str  # "call" | "put"
    bid: float | None = None
    ask: float | None = None
    last_price: float | None = None
    mid: float | None = None
    volume: int | None = None  # contracts traded today
    open_interest: int | None = None  # contracts outstanding (prior-day)
    implied_volatility: float | None = None  # percent
    in_the_money: bool | None = None
    premium: float | None = None  # dollars traded today (mid × volume × 100)
    volume_oi_ratio: float | None = None  # volume / open interest
    unusual: bool = False  # volume > open interest


class OptionsFlowSummaryResponse(BaseModel):
    call_volume: int
    put_volume: int
    total_volume: int
    call_open_interest: int
    put_open_interest: int
    put_call_volume_ratio: float | None = None
    put_call_oi_ratio: float | None = None
    call_premium: float  # dollars into calls
    put_premium: float  # dollars into puts
    net_premium: float  # call_premium - put_premium (signed)


class OptionsFlowResponse(BaseModel):
    ticker: str
    spot: float | None = None
    expiration: date | None = None  # null only when the symbol lists no options
    expirations: list[date] = []
    summary: OptionsFlowSummaryResponse | None = None  # null only when no options listed
    calls: list[OptionContractResponse] = []
    puts: list[OptionContractResponse] = []
    unusual: list[OptionContractResponse] = []  # capped, most money first
