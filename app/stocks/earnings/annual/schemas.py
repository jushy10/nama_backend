from datetime import date

from pydantic import BaseModel


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


class AnnualEarningsResponse(BaseModel):
    symbol: str
    count: int
    reported_count: int
    upcoming_count: int
    revenue_growth_yoy: float | None
    eps_growth_yoy: float | None
    fcf_growth_yoy: float | None
    years: list[AnnualEarningsYearResponse]
