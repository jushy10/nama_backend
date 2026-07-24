from datetime import date

from pydantic import BaseModel

from app.domains.financials.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)


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

    @classmethod
    def from_quarter(cls, quarter: QuarterlyEarnings) -> "QuarterlyEarningsQuarterResponse":
        return cls(
            fiscal_year=quarter.fiscal_year,
            fiscal_quarter=quarter.fiscal_quarter,
            period_end=quarter.period_end,
            report_date=quarter.report_date,
            eps_actual=quarter.eps_actual,
            eps_estimate=quarter.eps_estimate,
            eps_surprise=quarter.eps_surprise,
            eps_surprise_percent=quarter.eps_surprise_percent,
            revenue_estimate=quarter.revenue_estimate,
            revenue_actual=quarter.revenue_actual,
            report_session=quarter.report_session.value,
            beat=quarter.beat,
            is_reported=quarter.is_reported,
        )


class QuarterlyEarningsResponse(BaseModel):
    symbol: str
    count: int
    reported_count: int
    upcoming_count: int
    quarters: list[QuarterlyEarningsQuarterResponse]

    @classmethod
    def from_timeline(cls, timeline: QuarterlyEarningsTimeline) -> "QuarterlyEarningsResponse":
        return cls(
            symbol=timeline.symbol,
            count=len(timeline.quarters),
            reported_count=len(timeline.past),
            upcoming_count=len(timeline.future),
            quarters=[
                QuarterlyEarningsQuarterResponse.from_quarter(q)
                for q in timeline.quarters
            ],
        )
