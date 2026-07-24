from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.domains.financials.earnings_calendar import wiring
from app.domains.financials.earnings_calendar.api_schemas import EarningsCalendarResponse
from app.domains.financials.earnings_calendar.use_cases import GetEarningsCalendar

router = APIRouter(tags=["earnings-calendar"])


def get_get_earnings_calendar(db: Session = Depends(get_db)) -> GetEarningsCalendar:
    # Depends shim over the slice's wiring — exists for the db lifecycle and the
    # dependency_overrides test seam, nothing more.
    return wiring.build_get_earnings_calendar(db)


@router.get("/market/earnings-calendar", response_model=EarningsCalendarResponse)
def get_earnings_calendar_endpoint(
    response: Response,
    from_: date | None = Query(
        None,
        alias="from",
        description="Window start (YYYY-MM-DD). Defaults to today.",
    ),
    to: date | None = Query(
        None,
        description=(
            "Window end (YYYY-MM-DD). Defaults to two weeks past the start; the window is "
            "clamped to a maximum span."
        ),
    ),
    use_case: GetEarningsCalendar = Depends(get_get_earnings_calendar),
) -> EarningsCalendarResponse:
    try:
        calendar = use_case.run(from_, to)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Slow-moving stored dates refreshed out of band by the quarterly sync — cache half an hour.
    response.headers["Cache-Control"] = "public, max-age=1800"
    return EarningsCalendarResponse.from_calendar(calendar)
