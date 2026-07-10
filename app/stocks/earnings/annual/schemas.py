"""HTTP response DTOs for the annual-earnings endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. ``is_reported`` is
surfaced as a plain field (it's computed on the entity) so a client doesn't have to
re-derive it. There is no ``beat`` / surprise: Yahoo publishes no historical annual estimate
to compare a reported year against.
"""

from datetime import date

from pydantic import BaseModel


class AnnualEarningsYearResponse(BaseModel):
    """One fiscal year: the forward estimate for an upcoming year, or the actuals for a
    reported one."""

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
    """A stock's per-year earnings timeline: recent reported years then upcoming ones, in
    chronological order, with counts so a client can split the two without re-deriving them.

    ``revenue_growth_yoy`` / ``eps_growth_yoy`` / ``fcf_growth_yoy`` are the latest *trailing*
    year-over-year growth (percent) — the newest reported year over the one before it. Trailing
    (reported actuals, not estimates); the EPS figure is on the analyst-consensus (adjusted)
    basis, and FCF is on a per-share basis. ``None`` until two reported years are present (and
    each is best-effort beyond that — FCF growth needs the cash-flow figures on both years)."""

    symbol: str
    count: int
    reported_count: int
    upcoming_count: int
    revenue_growth_yoy: float | None
    eps_growth_yoy: float | None
    fcf_growth_yoy: float | None
    years: list[AnnualEarningsYearResponse]
