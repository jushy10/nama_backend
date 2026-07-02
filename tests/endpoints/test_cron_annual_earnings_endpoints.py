"""Tests for the annual-earnings cron endpoint (POST /internal/earnings/annual/sync).

Offline: a fake SyncAnnualEarnings (and a canned seed list) is injected through
dependency_overrides, so this checks only the controller — that it invokes the use case
with the requested limit, passes the constituent seeds through (or withholds them when
seeding is turned off), presents the summary, and validates the limit — without touching
Yahoo or the database. The seed-target wiring itself is checked against an in-memory
constituents table.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.constituents import ConstituentRecord
from app.stocks.earnings.annual.repository import RefreshTarget
from app.stocks.earnings.annual.use_cases import (
    AnnualEarningsSyncReport,
    SyncAnnualEarnings,
)
from app.stocks.endpoints import cron_annual_earnings_endpoints as cron

_SEEDS = (RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("MSFT", "Microsoft"))


class _FakeSync:
    """Stands in for SyncAnnualEarnings; records the limit + seeds it was called with."""

    def __init__(self, report: AnnualEarningsSyncReport) -> None:
        self._report = report
        self.calls: list[tuple[int | None, tuple[RefreshTarget, ...]]] = []

    def execute(
        self, *, limit: int | None = None, seeds=()
    ) -> AnnualEarningsSyncReport:
        self.calls.append((limit, tuple(seeds)))
        return self._report


def _client(fake: _FakeSync, seeds: tuple[RefreshTarget, ...] = _SEEDS) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_annual_earnings] = lambda: fake
    app.dependency_overrides[cron.get_seed_targets] = lambda: seeds
    return TestClient(app)


def test_runs_the_sync_and_returns_the_summary():
    fake = _FakeSync(AnnualEarningsSyncReport(refreshed=7, failed=2, limit=50, seeded=3))
    resp = _client(fake).post("/internal/earnings/annual/sync?limit=50")
    assert resp.status_code == 200
    assert resp.json() == {"refreshed": 7, "failed": 2, "limit": 50, "seeded": 3}
    assert fake.calls == [(50, _SEEDS)]  # limit + seeds both reached the use case


def test_defaults_the_limit_when_omitted():
    default = SyncAnnualEarnings.DEFAULT_LIMIT
    fake = _FakeSync(AnnualEarningsSyncReport(refreshed=0, failed=0, limit=default))
    resp = _client(fake).post("/internal/earnings/annual/sync")
    assert resp.status_code == 200
    assert fake.calls == [(default, _SEEDS)]


def test_seeding_can_be_disabled():
    fake = _FakeSync(AnnualEarningsSyncReport(refreshed=0, failed=0, limit=200))
    resp = _client(fake).post("/internal/earnings/annual/sync?seed_constituents=false")
    assert resp.status_code == 200
    assert fake.calls == [(200, ())]  # flag off -> no seeds reach the use case


def test_rejects_an_out_of_range_limit():
    fake = _FakeSync(AnnualEarningsSyncReport(0, 0, 1))
    # limit must be >= 1; 0 fails validation before the use case is invoked.
    assert (
        _client(fake).post("/internal/earnings/annual/sync?limit=0").status_code == 422
    )
    assert fake.calls == []


def test_sync_is_wired_without_any_api_key():
    # yfinance needs no credential, so the real DI builds the use case with no key set.
    use_case = cron.get_sync_annual_earnings(db=None)
    assert isinstance(use_case, SyncAnnualEarnings)


def test_seed_targets_come_from_constituents_and_skip_unfetchable_symbols():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                ConstituentRecord(
                    symbol="AAPL", name="Apple Inc.", sector="Information Technology",
                    in_sp500=True, in_nasdaq100=True,
                ),
                # Dotted share class: Yahoo wants a different spelling, and the read
                # path rejects it too — seeding it would only burn the run's budget.
                ConstituentRecord(
                    symbol="BRK.B", name="Berkshire Hathaway", sector="Financials",
                    in_sp500=True, in_nasdaq100=False,
                ),
            ]
        )
        db.commit()
        assert cron.get_seed_targets(db) == (RefreshTarget("AAPL", "Apple Inc."),)
