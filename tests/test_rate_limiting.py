from datetime import datetime, timezone

from starlette.testclient import TestClient

from app.main import app, limiter
from app.domains.research.analysis.entities import MarketSummary, MarketTone, SectorAnalysis
from app.endpoints.analysis_endpoints import (
    _AI_ANALYSIS_RATE_LIMIT,
    get_market_summary,
    get_sector_analysis,
)

# The per-endpoint allowance the AI-analysis routes carry (e.g. "10/minute"), parsed
# from the same constant the endpoints decorate with so this test tracks the default.
_AI_LIMIT_PER_WINDOW = int(_AI_ANALYSIS_RATE_LIMIT.split("/")[0])


def _hammer(client: TestClient, ip: str, n: int) -> list[int]:
    headers = {"X-Client-IP": ip}
    return [client.get("/healthz", headers=headers).status_code for _ in range(n)]


def test_bursting_past_the_per_ip_limit_gets_throttled():
    limiter.enabled = True
    client = TestClient(app)

    # 50 is comfortably past twice the 20/second allowance, so some requests are
    # throttled wherever the burst falls relative to the window boundary.
    codes = _hammer(client, "203.0.113.10", 50)

    assert 200 in codes  # the client isn't blocked outright...
    assert 429 in codes  # ...but its burst is throttled
    assert codes.count(200) <= 40  # ~20/second (at most two adjacent windows)


def test_each_client_ip_gets_its_own_allowance():
    # The whole point of keying on the client IP: one abusive caller burning its
    # allowance must not throttle a different client.
    limiter.enabled = True
    client = TestClient(app)

    abuser = _hammer(client, "203.0.113.20", 50)
    bystander = _hammer(client, "203.0.113.21", 5)

    assert 429 in abuser  # one IP exhausts its own bucket...
    assert bystander == [200] * 5  # ...the next IP is unaffected


# --------------------- the tighter per-endpoint AI limit ---------------------
#
# The AI-analysis reads each make a metered Bedrock call on a cache miss, so they
# carry a tight per-IP limit (``@limiter.limit(_AI_ANALYSIS_RATE_LIMIT)``) layered
# on top of the app-wide default limits. These drive the two AI endpoints with the
# lightest dependencies (the market-wide reads) through fakes so the limit — not a
# missing Bedrock/DB dependency — is what's under test.


class _FakeUseCase:
    def __init__(self, result):
        self._result = result

    def execute(self, *args, **kwargs):
        return self._result


def _a_market_summary() -> MarketSummary:
    return MarketSummary(
        summary="Markets drifted higher.",
        tone=MarketTone.RISK_ON,
        periods=(),
        model="test-model",
        generated_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )


def _a_sector_analysis() -> SectorAnalysis:
    return SectorAnalysis(
        summary="Tech led; utilities lagged.",
        tone=MarketTone.RISK_ON,
        leaders=(),
        laggards=(),
        model="test-model",
        generated_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )


def test_ai_analysis_endpoint_throttles_past_its_own_tighter_limit():
    # An AI read is far cheaper to abuse than a plain read (a metered model call per
    # miss), so it must throttle well below the generic 20/s + 600/min default. The
    # burst here stays under those app-wide limits, so it's the per-endpoint bucket —
    # not the default — that does the throttling.
    limiter.enabled = True
    app.dependency_overrides[get_market_summary] = lambda: _FakeUseCase(
        _a_market_summary()
    )
    try:
        client = TestClient(app)
        headers = {"X-Client-IP": "203.0.113.30"}
        codes = [
            client.get("/market/summary", headers=headers).status_code
            for _ in range(_AI_LIMIT_PER_WINDOW + 3)
        ]
    finally:
        app.dependency_overrides.clear()

    assert 200 in codes  # the client isn't blocked outright...
    assert 429 in codes  # ...but its burst is throttled at the tight AI limit
    assert codes.count(200) <= _AI_LIMIT_PER_WINDOW  # no more than the allowance


def test_each_ai_analysis_endpoint_has_its_own_bucket():
    # Each AI endpoint is scoped separately, so exhausting one must not spill over
    # onto another — a user reading one card shouldn't lock themselves out of the rest.
    limiter.enabled = True
    app.dependency_overrides[get_market_summary] = lambda: _FakeUseCase(
        _a_market_summary()
    )
    app.dependency_overrides[get_sector_analysis] = lambda: _FakeUseCase(
        _a_sector_analysis()
    )
    try:
        client = TestClient(app)
        headers = {"X-Client-IP": "203.0.113.31"}
        # Burn down the market-summary bucket for this IP...
        market = [
            client.get("/market/summary", headers=headers).status_code
            for _ in range(_AI_LIMIT_PER_WINDOW + 3)
        ]
        # ...then the same IP hits a *different* AI endpoint (its own, untouched bucket).
        sector = client.get("/sectors/analysis", headers=headers).status_code
    finally:
        app.dependency_overrides.clear()

    assert 429 in market  # one endpoint's bucket is exhausted...
    assert sector == 200  # ...the sibling endpoint's is unaffected
