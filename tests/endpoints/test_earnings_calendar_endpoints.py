from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from app.main import app
from app.stocks.company.earnings.quarterly.entities import EarningsSession
from app.stocks.market.earnings_calendar.entities import (
    EarningsCalendar,
    EarningsCalendarDay,
    EarningsCalendarItem,
)
from app.stocks.endpoints import earnings_calendar_endpoints as endpoints


class _FakeUseCase:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls: list[tuple] = []

    def execute(self, from_date=None, to_date=None):
        self.calls.append((from_date, to_date))
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app.dependency_overrides[endpoints.get_earnings_calendar_use_case] = lambda: fake
    return TestClient(app)


def _teardown():
    app.dependency_overrides.pop(endpoints.get_earnings_calendar_use_case, None)


def _a_calendar() -> EarningsCalendar:
    return EarningsCalendar(
        from_date=date(2026, 7, 15),
        to_date=date(2026, 7, 29),
        days=(
            EarningsCalendarDay(
                date=date(2026, 7, 20),
                items=(
                    EarningsCalendarItem(
                        "AAPL", "Apple", "technology", date(2026, 7, 20),
                        EarningsSession.AMC, market_cap=3.4e12,
                    ),
                    EarningsCalendarItem(
                        "MSFT", "Microsoft", "technology", date(2026, 7, 20),
                        EarningsSession.BMO,
                    ),
                ),
            ),
        ),
    )


def test_returns_the_expected_shape():
    fake = _FakeUseCase(result=_a_calendar())
    try:
        resp = _client(fake).get("/market/earnings-calendar")
        assert resp.status_code == 200
        body = resp.json()
        assert body["from"] == "2026-07-15"
        assert body["to"] == "2026-07-29"
        assert body["count"] == 2
        day = body["days"][0]
        assert day["date"] == "2026-07-20"
        assert day["count"] == 2
        item = day["items"][0]
        assert item == {
            "ticker": "AAPL",
            "name": "Apple",
            "sector": "technology",
            "when": "2026-07-20",  # the item's scheduled report date
            "session": "amc",  # after market close
            "market_cap": 3.4e12,
        }
        assert day["items"][1]["session"] == "bmo"  # before market open
        # A not-yet-screened symbol has no market cap.
        assert day["items"][1]["market_cap"] is None
        assert "not financial advice" in body["disclaimer"]
        assert resp.headers["cache-control"] == "public, max-age=1800"
    finally:
        _teardown()


def test_passes_the_from_and_to_params():
    fake = _FakeUseCase(result=_a_calendar())
    try:
        _client(fake).get("/market/earnings-calendar?from=2026-07-15&to=2026-07-20")
        assert fake.calls == [(date(2026, 7, 15), date(2026, 7, 20))]
    finally:
        _teardown()


def test_defaults_to_no_window_params():
    fake = _FakeUseCase(result=_a_calendar())
    try:
        _client(fake).get("/market/earnings-calendar")
        assert fake.calls == [(None, None)]
    finally:
        _teardown()


def test_inverted_window_is_400():
    fake = _FakeUseCase(error=ValueError("'to' must not be before 'from'."))
    try:
        resp = _client(fake).get("/market/earnings-calendar?from=2026-07-20&to=2026-07-10")
        assert resp.status_code == 400
    finally:
        _teardown()


def test_malformed_date_is_422():
    # A non-ISO query date fails FastAPI's own validation before the use case.
    fake = _FakeUseCase(result=_a_calendar())
    try:
        resp = _client(fake).get("/market/earnings-calendar?from=nonsense")
        assert resp.status_code == 422
        assert fake.calls == []
    finally:
        _teardown()
