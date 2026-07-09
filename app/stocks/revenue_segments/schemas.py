"""HTTP response DTOs for the revenue-segments endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. ``axis`` serializes as
its slug (``"product"``) and ``label`` as the derived human rendering of the raw ``member``
(both computed on the entity), so a client doesn't have to re-derive either.
"""

from datetime import date

from pydantic import BaseModel


class RevenueSegmentResponse(BaseModel):
    """One revenue figure for one (fiscal year, axis, member).

    ``axis`` is the disaggregation cut (``business_segment`` / ``product`` / ``geography``);
    ``member`` is the filer's raw label and ``label`` its human rendering; ``value`` is revenue
    in the filing's reporting currency (raw, typically USD)."""

    fiscal_year: int
    period_end: date | None
    axis: str
    member: str
    label: str
    value: float


class RevenueSegmentationResponse(BaseModel):
    """A company's revenue disaggregation: every stored (year, axis, member) figure, newest
    fiscal year first.

    ``fiscal_years`` lists the distinct years on file (newest first) and ``latest_fiscal_year``
    the most recent, so a client can pick a year without scanning ``segments``. An empty
    ``segments`` means the company reports no disaggregation. Members are the filer's own labels
    — comparable within this company over time, but not across companies."""

    symbol: str
    count: int
    fiscal_years: list[int]
    latest_fiscal_year: int | None
    segments: list[RevenueSegmentResponse]
