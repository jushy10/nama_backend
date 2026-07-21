from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.stocks.brief.entities import BriefTone, MarketBrief, MarketBriefSection
from app.stocks.endpoints import market_brief_endpoints as endpoints


class _FakeUseCase:
    def __init__(self, result=None):
        self._result = result
        self.dates: list[date | None] = []

    def execute(self, brief_date=None):
        self.dates.append(brief_date)
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app.dependency_overrides[endpoints.get_daily_brief_use_case] = lambda: fake
    return TestClient(app)


def _teardown():
    app.dependency_overrides.pop(endpoints.get_daily_brief_use_case, None)


def _a_brief(day=date(2026, 7, 14)) -> MarketBrief:
    return MarketBrief(
        brief_date=day,
        generated_at=datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc),
        tone=BriefTone.RISK_ON,
        summary="A broad rally, led by tech.",
        sections=(
            MarketBriefSection("Overview", "Stocks climbed across the board."),
            MarketBriefSection("Sectors", "Technology led."),
        ),
        model="test-model",
    )


def test_latest_returns_the_expected_shape():
    fake = _FakeUseCase(result=_a_brief())
    try:
        resp = _client(fake).get("/market/brief")
        assert resp.status_code == 200
        body = resp.json()
        assert body["date"] == "2026-07-14"
        assert body["tone"] == "risk_on"
        assert body["summary"].startswith("A broad rally")
        assert [s["heading"] for s in body["sections"]] == ["Overview", "Sectors"]
        assert body["model"] == "test-model"
        assert "not financial advice" in body["disclaimer"]
        assert fake.dates == [None]  # latest -> no date
        assert resp.headers["cache-control"] == "public, max-age=900"
    finally:
        _teardown()


def test_latest_is_404_before_any_brief_exists():
    try:
        assert _client(_FakeUseCase(result=None)).get("/market/brief").status_code == 404
    finally:
        _teardown()


def test_dated_brief_parses_and_passes_the_date():
    fake = _FakeUseCase(result=_a_brief(date(2026, 7, 10)))
    try:
        resp = _client(fake).get("/market/brief/2026-07-10")
        assert resp.status_code == 200
        assert resp.json()["date"] == "2026-07-10"
        assert fake.dates == [date(2026, 7, 10)]
    finally:
        _teardown()


def test_dated_brief_missing_is_404():
    try:
        resp = _client(_FakeUseCase(result=None)).get("/market/brief/2020-01-01")
        assert resp.status_code == 404
    finally:
        _teardown()


def test_malformed_date_is_400():
    fake = _FakeUseCase(result=None)
    try:
        resp = _client(fake).get("/market/brief/not-a-date")
        assert resp.status_code == 400
        assert fake.dates == []  # never reached the use case
    finally:
        _teardown()
