import datetime

from pydantic import BaseModel


class YieldTenorResponse(BaseModel):
    label: str
    months: float
    rate: float


class YieldCurveResponse(BaseModel):
    as_of: datetime.date
    two_year: float | None = None
    ten_year: float | None = None
    # 10Y minus 2Y, in percentage points; negative == inverted curve.
    spread_2s10s: float | None = None
    is_inverted: bool | None = None
    count: int
    tenors: list[YieldTenorResponse]


class YieldObservationResponse(BaseModel):
    date: datetime.date
    rate: float


class YieldSeriesResponse(BaseModel):
    label: str
    observations: list[YieldObservationResponse]


class YieldHistoryResponse(BaseModel):
    latest_spread: float | None = None
    is_inverted: bool | None = None
    series: list[YieldSeriesResponse]
    # The 10Y-2Y spread on every date both maturities were quoted; the point it
    # crosses below zero is where the curve inverts.
    spread: list[YieldObservationResponse]
