"""Tests for the universe cron endpoint (POST /internal/universe/sync).

Offline: a fake SyncUniverse is injected through dependency_overrides, so this checks only
the controller — it invokes the use case, presents the summary, and maps a hard screen
failure to 502 — without touching Yahoo or the database.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import cron_universe_endpoints as cron
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.use_cases import SyncUniverse, UniverseSyncReport


class _FakeSync:
    """Stands in for SyncUniverse; records how many times it ran."""

    def __init__(self, report: UniverseSyncReport | None = None, *, error=None) -> None:
        self._report = report
        self._error = error
        self.calls = 0

    def execute(self) -> UniverseSyncReport:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._report


def _client(fake: _FakeSync) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_universe] = lambda: fake
    return TestClient(app)


def test_runs_the_sync_and_returns_the_summary():
    fake = _FakeSync(
        UniverseSyncReport(screened=1200, added=30, updated=1170, skipped=False)
    )
    resp = _client(fake).post("/internal/universe/sync")
    assert resp.status_code == 200
    assert resp.json() == {
        "screened": 1200,
        "added": 30,
        "updated": 1170,
        "skipped": False,
    }
    assert fake.calls == 1


def test_reports_a_skipped_screen_as_a_200():
    fake = _FakeSync(
        UniverseSyncReport(screened=0, added=0, updated=0, skipped=True)
    )
    resp = _client(fake).post("/internal/universe/sync")
    assert resp.status_code == 200
    assert resp.json()["skipped"] is True


def test_hard_screen_failure_maps_to_502():
    fake = _FakeSync(error=StockDataUnavailable("*", "yahoo blocked"))
    resp = _client(fake).post("/internal/universe/sync")
    assert resp.status_code == 502
    assert fake.calls == 1


def test_sync_is_wired_without_any_api_key():
    # Yahoo's screener needs no credential, so the real DI builds the use case with no key.
    use_case = cron.get_sync_universe(db=None)
    assert isinstance(use_case, SyncUniverse)
