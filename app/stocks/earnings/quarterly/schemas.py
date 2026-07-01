"""HTTP response DTOs for the quarterly-earnings endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. ``beat`` and
``is_reported`` are surfaced as plain fields (they're computed on the entity) so a client
doesn't have to re-derive them.
"""

from datetime import date

from pydantic import BaseModel


class QuarterlyEarningsQuarterResponse(BaseModel):
    """One fiscal quarter: the estimate going in and, once reported, the actual."""

    fiscal_year: int
    fiscal_quarter: int
    period_end: date | None
    report_date: date | None
    eps_actual: float | None
    eps_estimate: float | None
    eps_surprise: float | None
    eps_surprise_percent: float | None
    revenue_estimate: float | None
    beat: bool | None
    is_reported: bool


class QuarterlyEarningsResponse(BaseModel):
    """A stock's per-quarter earnings timeline: recent reported quarters then upcoming
    ones, with counts so a client can split the two without re-deriving them."""

    symbol: str
    count: int
    reported_count: int
    upcoming_count: int
    quarters: list[QuarterlyEarningsQuarterResponse]
