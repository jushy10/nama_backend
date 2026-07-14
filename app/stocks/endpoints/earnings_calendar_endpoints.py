"""HTTP API for the market-wide earnings calendar.

``GET /market/earnings-calendar?from=&to=`` — which companies are scheduled to report earnings
on which upcoming days, aggregated across the screened universe and grouped by day. Served
**DB-only** from the scheduled dates the quarterly-earnings sync already stores, so a read is
one indexed query, never a vendor call. Controller + presenter + wiring, the composition-root
way.

Best-effort: an empty or quiet window is a 200 with no days, not a 404. ``from``/``to`` default
to a two-week look-ahead and the window is clamped in the use case; an inverted window is a 400.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.earnings_calendar.db_repository import SqlEarningsCalendarRepository
from app.stocks.earnings_calendar.entities import EarningsCalendar
from app.stocks.earnings_calendar.schemas import (
    EarningsCalendarDayResponse,
    EarningsCalendarItemResponse,
    EarningsCalendarResponse,
)
from app.stocks.earnings_calendar.use_cases import GetEarningsCalendar

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
    return GetEarningsCalendar(SqlEarningsCalendarRepository(db))


def _present(calendar: EarningsCalendar) -> EarningsCalendarResponse:
    """Presenter: calendar entity -> HTTP response DTO (each item's ``report_date`` -> ``when``)."""
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
    """The upcoming earnings calendar. An inverted window (``to`` before ``from``) is a 400."""
    try:
        calendar = use_case.execute(from_, to)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Slow-moving stored dates refreshed out of band by the quarterly sync — cache half an hour.
    response.headers["Cache-Control"] = "public, max-age=1800"
    return _present(calendar)
