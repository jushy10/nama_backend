"""The earnings-calendar slice's composition root — framework-free. A pure DB read
over the quarterly slice's stored scheduled dates (no vendor, no key), so the builder
constructs the db-backed repository from the Session itself."""

from sqlalchemy.orm import Session

from app.domains.financials.earnings_calendar.db_repository import (
    DbEarningsCalendarRepository,
)
from app.domains.financials.earnings_calendar.use_cases import GetEarningsCalendar


def build_get_earnings_calendar(db: Session) -> GetEarningsCalendar:
    return GetEarningsCalendar(DbEarningsCalendarRepository(db))
