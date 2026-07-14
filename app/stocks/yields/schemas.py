"""HTTP response models for the Treasury-yields endpoints.

Pydantic is a web/serialization detail, so these DTOs live at the edge —
deliberately separate from the entities so the core stays framework-agnostic.
The derived reads (spread, inversion flag) are surfaced top-level so a client
doesn't recompute what the entity already knows.
"""

import datetime

from pydantic import BaseModel


class YieldTenorResponse(BaseModel):
    """One maturity point on the curve: display label, tenor (months), yield %."""

    label: str
    months: float
    rate: float


class YieldCurveResponse(BaseModel):
    """The current US Treasury par-yield curve, shortest maturity first."""

    as_of: datetime.date
    two_year: float | None = None
    ten_year: float | None = None
    # 10Y minus 2Y, in percentage points; negative == inverted curve.
    spread_2s10s: float | None = None
    is_inverted: bool | None = None
    count: int
    tenors: list[YieldTenorResponse]


class YieldObservationResponse(BaseModel):
    """One maturity's yield on one date."""

    date: datetime.date
    rate: float


class YieldSeriesResponse(BaseModel):
    """One maturity's yield over time (e.g. the 2Y), oldest observation first."""

    label: str
    observations: list[YieldObservationResponse]


class YieldHistoryResponse(BaseModel):
    """The 2Y and 10Y yields over time, plus the derived 2s10s spread series."""

    latest_spread: float | None = None
    is_inverted: bool | None = None
    series: list[YieldSeriesResponse]
    # The 10Y-2Y spread on every date both maturities were quoted; the point it
    # crosses below zero is where the curve inverts.
    spread: list[YieldObservationResponse]
