from datetime import date

from pydantic import BaseModel


class RevenueSegmentResponse(BaseModel):
    fiscal_year: int
    period_end: date | None
    axis: str
    member: str
    label: str
    value: float


class RevenueSegmentationResponse(BaseModel):
    symbol: str
    count: int
    fiscal_years: list[int]
    latest_fiscal_year: int | None
    segments: list[RevenueSegmentResponse]
