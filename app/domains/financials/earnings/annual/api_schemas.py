from datetime import date

from pydantic import BaseModel

from app.domains.financials.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)


class AnnualEarningsYearResponse(BaseModel):
    fiscal_year: int
    period_end: date | None
    eps_actual: float | None
    eps_estimate: float | None
    revenue_actual: float | None
    revenue_estimate: float | None
    net_income: float | None
    # The reported year's actual EPS on the analyst-consensus (adjusted) basis — comparable
    # with eps_estimate, unlike the GAAP-diluted eps_actual. Best-effort; reported years only.
    eps_actual_consensus: float | None
    # The reported year's free- and operating-cash-flow per share (trading currency), from the
    # cash-flow statement over the year's diluted average shares. Best-effort; reported years
    # only (an upcoming year carries neither) — so a client can chart an FCF/share trend.
    fcf_per_share: float | None
    ocf_per_share: float | None
    is_reported: bool

    @classmethod
    def from_year(cls, year: AnnualEarnings) -> "AnnualEarningsYearResponse":
        return cls(
            fiscal_year=year.fiscal_year,
            period_end=year.period_end,
            eps_actual=year.eps_actual,
            eps_estimate=year.eps_estimate,
            revenue_actual=year.revenue_actual,
            revenue_estimate=year.revenue_estimate,
            net_income=year.net_income,
            eps_actual_consensus=year.eps_actual_consensus,
            fcf_per_share=year.fcf_per_share,
            ocf_per_share=year.ocf_per_share,
            is_reported=year.is_reported,
        )


class AnnualEarningsResponse(BaseModel):
    symbol: str
    count: int
    reported_count: int
    upcoming_count: int
    revenue_growth_yoy: float | None
    eps_growth_yoy: float | None
    fcf_growth_yoy: float | None
    years: list[AnnualEarningsYearResponse]

    @classmethod
    def from_timeline(cls, timeline: AnnualEarningsTimeline) -> "AnnualEarningsResponse":
        return cls(
            symbol=timeline.symbol,
            count=len(timeline.years),
            reported_count=len(timeline.past),
            upcoming_count=len(timeline.future),
            revenue_growth_yoy=timeline.latest_revenue_growth_yoy,
            eps_growth_yoy=timeline.latest_eps_growth_yoy,
            fcf_growth_yoy=timeline.latest_fcf_growth_yoy,
            years=[AnnualEarningsYearResponse.from_year(y) for y in timeline.years],
        )
