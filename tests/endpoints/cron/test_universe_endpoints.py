from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints.cron import universe_endpoints as cron
from app.stocks.catalog.universe.use_cases import SyncUniverse, UniverseSyncReport


class _FakeRunner:
    def __init__(self, report: UniverseSyncReport) -> None:
        self._report = report
        self.calls: list[int | None] = []

    def __call__(self, limit: int | None = None) -> UniverseSyncReport:
        self.calls.append(limit)
        return self._report


def _report() -> UniverseSyncReport:
    return UniverseSyncReport(
        screened=1200,
        added=30,
        updated=1170,
        skipped=False,
        enriched=40,
        enrich_failed=2,
        valued=38,
    )


def _client(fake: _FakeRunner) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_runner] = lambda: fake
    # The auth guard is covered on its own in test_cron_auth.py; no-op it here so these
    # controller tests don't need a token.
    app.dependency_overrides[cron.require_cron_token] = lambda: None
    return TestClient(app)


def _drain() -> None:
    assert cron._sync_lock.acquire(timeout=2), "background sweep did not finish in time"
    cron._sync_lock.release()


def test_accepts_the_trigger_and_runs_the_sweep_with_the_limit():
    fake = _FakeRunner(_report())
    resp = _client(fake).post("/internal/universe/sync?limit=50")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": 50}
    _drain()
    assert fake.calls == [50]  # the query limit reached the runner


def test_defaults_the_limit_when_omitted():
    default = SyncUniverse.DEFAULT_LIMIT
    fake = _FakeRunner(_report())
    resp = _client(fake).post("/internal/universe/sync")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": default}
    _drain()
    assert fake.calls == [default]


def test_a_trigger_while_a_sweep_runs_is_a_noop():
    fake = _FakeRunner(_report())
    # Simulate a sweep in flight by holding the guard, so the endpoint can't start another.
    assert cron._sync_lock.acquire(blocking=False)
    try:
        resp = _client(fake).post("/internal/universe/sync?limit=50")
        assert resp.status_code == 200
        assert resp.json() == {"status": "already_running", "limit": 50}
        assert fake.calls == []  # nothing started while one was running
    finally:
        cron._sync_lock.release()


def test_rejects_an_out_of_range_limit():
    fake = _FakeRunner(_report())
    # limit must be >= 1; 0 fails validation before anything is scheduled.
    assert _client(fake).post("/internal/universe/sync?limit=0").status_code == 422
    assert fake.calls == []
    # The guard must be free — a rejected request must never strand it.
    assert cron._sync_lock.acquire(blocking=False)
    cron._sync_lock.release()


def _pass(*, screened, added, updated, skipped, enriched=0, enrich_failed=0, valued=0):
    return UniverseSyncReport(
        screened=screened, added=added, updated=updated, skipped=skipped,
        enriched=enriched, enrich_failed=enrich_failed, valued=valued,
    )


def test_merge_reports_sums_counts_across_market_passes():
    # US pass + CA pass -> one report: counts summed, and NOT skipped because at least one
    # market was written.
    merged = cron._merge_reports(
        [
            _pass(screened=2800, added=10, updated=2790, skipped=False, enriched=5, valued=4),
            _pass(screened=250, added=250, updated=0, skipped=False, enriched=3, valued=2),
        ]
    )
    assert (merged.screened, merged.added, merged.updated) == (3050, 260, 2790)
    assert (merged.enriched, merged.valued) == (8, 6)
    assert merged.skipped is False


def test_merge_reports_is_skipped_only_when_every_pass_skipped():
    # A mixed run (US written, CA skipped) is not a skip; both skipped is.
    mixed = cron._merge_reports(
        [
            _pass(screened=2800, added=10, updated=2790, skipped=False),
            _pass(screened=5, added=0, updated=0, skipped=True),
        ]
    )
    assert mixed.skipped is False

    both = cron._merge_reports(
        [
            _pass(screened=5, added=0, updated=0, skipped=True),
            _pass(screened=3, added=0, updated=0, skipped=True),
        ]
    )
    assert both.skipped is True


def test_merge_reports_of_no_passes_is_a_skip():
    # Every pass raised (nothing ran) -> a skip, all-zero counts.
    empty = cron._merge_reports([])
    assert empty.skipped is True
    assert (empty.screened, empty.added, empty.updated, empty.valued) == (0, 0, 0, 0)


def test_runner_is_wired_without_any_api_key():
    # yfinance needs no credential, so the DI returns the real unit of work with no key set.
    assert cron.get_sync_runner() is cron.run_universe_sync
