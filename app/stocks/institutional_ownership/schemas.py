from datetime import date

from pydantic import BaseModel


class InstitutionalHolderResponse(BaseModel):
    holder: str
    holder_type: str
    date_reported: date
    shares: float | None
    value: float | None
    pct_held: float | None
    pct_change: float | None
    is_buyer: bool
    is_seller: bool
    share_change: float | None
    value_change: float | None


class OwnershipBreakdownResponse(BaseModel):
    institutions_pct_held: float | None
    insiders_pct_held: float | None
    institutions_float_pct_held: float | None
    institutions_count: int | None


class HolderFlowResponse(BaseModel):
    buyers_count: int
    sellers_count: int
    shares_bought: float
    shares_sold: float
    value_bought: float
    value_sold: float
    net_share_change: float
    net_value_change: float


class InstitutionalOwnershipResponse(BaseModel):
    symbol: str
    count: int
    latest_report_date: date | None = None
    breakdown: OwnershipBreakdownResponse | None = None
    flow: HolderFlowResponse
    holders: list[InstitutionalHolderResponse]
