"""CORS lets the SPA (a different origin) call the API from the browser."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SITE = "https://namainsights.com"


def test_simple_request_echoes_allow_origin():
    r = client.get("/healthz", headers={"Origin": SITE})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == SITE


def test_preflight_allows_get_on_stocks():
    r = client.options(
        "/stocks/AAPL",
        headers={"Origin": SITE, "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == SITE


def test_unlisted_origin_gets_no_cors_grant():
    r = client.get("/healthz", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 200  # the request still succeeds server-side...
    assert "access-control-allow-origin" not in r.headers  # ...but no CORS grant
