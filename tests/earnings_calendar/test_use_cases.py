from datetime import date, timedelta

import pytest

from app.stocks.earnings_calendar.entities import EarningsCalendarItem
from app.stocks.earnings_calendar.repository import EarningsCalendarRepository
from app.stocks.earnings_calendar.use_cases import GetEarningsCalendar

_TODAY = date(2026, 7, 14)


class _FakeRepository(EarningsCalendarRepository):
    def __init__(self, items=()):
        self._items = list(items)
        self.window: tuple[date, date, int] | None = None

    def upcoming(self, from_date, to_date, limit):
        self.window = (from_date, to_date, limit)
        return [i for i in self._items if from_date <= i.report_date <= to_date]


def _uc(repo) -> GetEarningsCalendar:
    return GetEarningsCalendar(repo, today=lambda: _TODAY)


def _item(ticker, day, name="X", sector="technology") -> EarningsCalendarItem:
    return EarningsCalendarItem(ticker=ticker, name=name, sector=sector, report_date=day)


def test_defaults_to_today_through_two_weeks():
    repo = _FakeRepository()
    _uc(repo).execute()
    start, end, _ = repo.window
    assert start == _TODAY
    assert end == _TODAY + timedelta(days=GetEarningsCalendar.DEFAULT_WINDOW_DAYS)


def test_groups_items_by_day_ordered_and_alphabetical():
    repo = _FakeRepository(
        [
            _item("MSFT", date(2026, 7, 20)),
            _item("AAPL", date(2026, 7, 20)),
            _item("NVDA", date(2026, 7, 18)),
        ]
    )
    cal = _uc(repo).execute()

    assert [d.date for d in cal.days] == [date(2026, 7, 18), date(2026, 7, 20)]
    # Same-day items are alphabetical by ticker.
    assert [i.ticker for i in cal.days[1].items] == ["AAPL", "MSFT"]
    assert cal.count == 3


def test_clamps_an_over_wide_window():
    repo = _FakeRepository()
    _uc(repo).execute(_TODAY, _TODAY + timedelta(days=365))
    _, end, _ = repo.window
    assert end == _TODAY + timedelta(days=GetEarningsCalendar.MAX_WINDOW_DAYS)


def test_inverted_window_is_an_error():
    with pytest.raises(ValueError):
        _uc(_FakeRepository()).execute(date(2026, 7, 20), date(2026, 7, 10))


def test_echoes_the_clamped_window_on_the_result():
    repo = _FakeRepository()
    cal = _uc(repo).execute(date(2026, 7, 1), date(2026, 7, 5))
    assert cal.from_date == date(2026, 7, 1)
    assert cal.to_date == date(2026, 7, 5)


def test_empty_window_is_a_calendar_with_no_days():
    cal = _uc(_FakeRepository()).execute()
    assert cal.days == ()
    assert cal.count == 0
