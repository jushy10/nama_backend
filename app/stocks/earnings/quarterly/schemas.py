from datetime import date

from pydantic import BaseModel


class QuarterlyEarningsQuarterResponse(BaseModel):
    fiscal_year: int
    fiscal_quarter: int
    period_end: date | None
    report_date: date | None
    eps_actual: float | None
    eps_estimate: float | None
    eps_surprise: float | None
    eps_surprise_percent: float | None
    revenue_estimate: float | None
    revenue_actual: float | None
    # Market timing of the announcement: "bmo" (before open) / "amc" (after close) /
    # "during" (intraday) / "unknown" (no usable time published).
    report_session: str
    beat: bool | None
    is_reported: bool


class QuarterlyEarningsResponse(BaseModel):
    symbol: str
    count: int
    reported_count: int
    upcoming_count: int
    quarters: list[QuarterlyEarningsQuarterResponse]
