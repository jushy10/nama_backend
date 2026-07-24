import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.endpoints import docs_endpoints

_USERNAME = "staff"
_PASSWORD = "s3cr3t-docs-pass"


def _client() -> TestClient:
    """A minimal app with one public and one internal route, plus the docs router."""
    app = FastAPI(title="testapp", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/stocks/example")
    def public_read() -> dict:  # pragma: no cover - never called
        return {}

    @app.post("/internal/example/sync")
    def internal_sync() -> dict:  # pragma: no cover - never called
        return {}

    app.include_router(docs_endpoints.router)
    return TestClient(app)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("INTERNAL_DOCS_USERNAME", _USERNAME)
    monkeypatch.setenv("INTERNAL_DOCS_PASSWORD", _PASSWORD)


# ── the public pair (no auth) ──────────────────────────────────────────────


def test_public_openapi_excludes_internal_routes():
    resp = _client().get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/stocks/example" in paths
    assert not any(p.startswith("/internal/") for p in paths)
    # The docs plumbing itself stays out of the schema (include_in_schema=False).
    assert "/docs" not in paths
    assert "/openapi.json" not in paths


def test_public_docs_page_serves_without_auth():
    resp = _client().get("/docs")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # The page points at the public schema, not the internal one.
    assert "/openapi.json" in resp.text
    assert "/internal/openapi.json" not in resp.text


# ── the internal pair (HTTP Basic, fail-closed) ────────────────────────────


def test_unset_credentials_are_fail_closed_503(monkeypatch):
    monkeypatch.delenv("INTERNAL_DOCS_USERNAME", raising=False)
    monkeypatch.delenv("INTERNAL_DOCS_PASSWORD", raising=False)
    # Even well-formed credentials are refused while the guard is unconfigured.
    resp = _client().get("/internal/openapi.json", auth=(_USERNAME, _PASSWORD))
    assert resp.status_code == 503


def test_missing_credentials_are_401_with_basic_challenge(configured):
    resp = _client().get("/internal/openapi.json")
    assert resp.status_code == 401
    # The Basic challenge is what makes a browser prompt for credentials.
    assert resp.headers.get("www-authenticate") == "Basic"


def test_wrong_password_is_401(configured):
    resp = _client().get("/internal/openapi.json", auth=(_USERNAME, "wrong"))
    assert resp.status_code == 401


def test_wrong_username_is_401(configured):
    resp = _client().get("/internal/openapi.json", auth=("intruder", _PASSWORD))
    assert resp.status_code == 401


def test_internal_openapi_serves_only_internal_routes(configured):
    resp = _client().get("/internal/openapi.json", auth=(_USERNAME, _PASSWORD))
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/internal/example/sync" in paths
    assert all(p.startswith("/internal/") for p in paths)


def test_internal_docs_page_is_guarded_too(configured):
    client = _client()
    assert client.get("/internal/docs").status_code == 401
    resp = client.get("/internal/docs", auth=(_USERNAME, _PASSWORD))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "/internal/openapi.json" in resp.text


# ── the real app ───────────────────────────────────────────────────────────


def test_real_app_public_schema_hides_the_cron_surface():
    from app.main import app

    resp = TestClient(app).get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert not any(p.startswith("/internal/") for p in paths)
    # A couple of known public reads are present.
    assert "/stocks/ticker/{ticker}" in paths
    assert "/market/sentiment" in paths


def test_real_app_internal_schema_is_the_cron_surface(configured):
    from app.main import app

    resp = TestClient(app).get("/internal/openapi.json", auth=(_USERNAME, _PASSWORD))
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert paths, "internal schema should not be empty"
    assert all(p.startswith("/internal/") for p in paths)
    assert "/internal/universe/sync" in paths
