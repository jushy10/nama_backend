from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.company.earnings.quarterly.use_cases import QuarterlyEarningsSyncReport
from app.stocks.endpoints.cron import quarterly_earnings_endpoints as cron

_TOKEN = "s3cr3t-cron-token"
_URL = "/internal/earnings/quarterly/sync"


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[int | None] = []

    def __call__(self, limit: int | None = None) -> QuarterlyEarningsSyncReport:
        self.calls.append(limit)
        return QuarterlyEarningsSyncReport(refreshed=0, failed=0, limit=limit)


def _client(fake: _FakeRunner) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    # Only the runner is faked — the guard runs for real, since it's what these tests cover.
    app.dependency_overrides[cron.get_sync_runner] = lambda: fake
    return TestClient(app)


def _drain() -> None:
    assert cron._sync_lock.acquire(timeout=2), "background sweep did not finish in time"
    cron._sync_lock.release()


def _assert_guard_not_stranded() -> None:
    assert cron._sync_lock.acquire(blocking=False)
    cron._sync_lock.release()


def test_unset_token_is_fail_closed_503(monkeypatch):
    monkeypatch.delenv("CRON_SYNC_TOKEN", raising=False)
    fake = _FakeRunner()
    # Even a well-formed bearer is refused while the guard is unconfigured — fail-closed.
    resp = _client(fake).post(_URL, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status_code == 503
    assert fake.calls == []
    _assert_guard_not_stranded()


def test_missing_authorization_header_is_401(monkeypatch):
    monkeypatch.setenv("CRON_SYNC_TOKEN", _TOKEN)
    fake = _FakeRunner()
    resp = _client(fake).post(_URL)
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert fake.calls == []
    _assert_guard_not_stranded()


def test_wrong_token_is_401(monkeypatch):
    monkeypatch.setenv("CRON_SYNC_TOKEN", _TOKEN)
    fake = _FakeRunner()
    resp = _client(fake).post(_URL, headers={"Authorization": "Bearer not-the-token"})
    assert resp.status_code == 401
    assert fake.calls == []
    _assert_guard_not_stranded()


def test_non_bearer_scheme_is_401(monkeypatch):
    monkeypatch.setenv("CRON_SYNC_TOKEN", _TOKEN)
    fake = _FakeRunner()
    # A Basic header carries no bearer credentials, so HTTPBearer yields None -> uniform 401.
    resp = _client(fake).post(_URL, headers={"Authorization": f"Basic {_TOKEN}"})
    assert resp.status_code == 401
    assert fake.calls == []
    _assert_guard_not_stranded()


def test_correct_token_is_accepted(monkeypatch):
    monkeypatch.setenv("CRON_SYNC_TOKEN", _TOKEN)
    fake = _FakeRunner()
    resp = _client(fake).post(_URL, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": None}
    _drain()
    assert fake.calls == [None]  # the guard let the trigger through to the runner
