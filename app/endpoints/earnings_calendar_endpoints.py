from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.domains.financials.earnings_calendar.earnings_calendar_repository_adapter_impl import EarningsCalendarRepositoryAdapterImpl
from app.domains.financials.earnings_calendar.entities import EarningsCalendar
from app.domains.financials.earnings_calendar.schemas import (
    EarningsCalendarDayResponse,
    EarningsCalendarItemResponse,
    EarningsCalendarResponse,
)
from app.domains.financials.earnings_calendar.use_cases import GetEarningsCalendar

router = APIRouter(tags=["earnings-calendar"])

# Informational, attached at the edge — a schedule of who reports when, not financial advice.
_DISCLAIMER = (
    "Scheduled earnings dates are estimates that can change and are for general information "
    "only — not financial advice."
)


def get_earnings_calendar_use_case(
    db: Session = Depends(get_db),
) -> GetEarningsCalendar:
    # Pure DB read over stored scheduled dates — no vendor, no key.
    return GetEarningsCalendar(EarningsCalendarRepositoryAdapterImpl(db))


def _present(calendar: EarningsCalendar) -> EarningsCalendarResponse:
    return EarningsCalendarResponse(
        from_=calendar.from_date,
        to=calendar.to_date,
        count=calendar.count,
        days=[
            EarningsCalendarDayResponse(
                date=day.date,
                count=len(day.items),
                items=[
                    EarningsCalendarItemResponse(
                        ticker=item.ticker,
                        name=item.name,
                        sector=item.sector,
                        when=item.report_date,
                        session=item.session.value,
                        market_cap=item.market_cap,
                    )
                    for item in day.items
                ],
            )
            for day in calendar.days
        ],
        disclaimer=_DISCLAIMER,
    )


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
    use_case: GetEarningsCalendar = Depends(get_earnings_calendar_use_case),
) -> EarningsCalendarResponse:
    try:
        calendar = use_case.execute(from_, to)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Slow-moving stored dates refreshed out of band by the quarterly sync — cache half an hour.
    response.headers["Cache-Control"] = "public, max-age=1800"
    return _present(calendar)
