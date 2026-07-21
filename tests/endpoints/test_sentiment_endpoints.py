from datetime import date, datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import sentiment_endpoints as endpoints
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.sentiment.entities import (
    FearGreedSnapshot,
    MarketSentiment,
    VixSnapshot,
)


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_market_sentiment] = lambda: fake
    return TestClient(app)


def _vix() -> VixSnapshot:
    return VixSnapshot(as_of=date(2026, 7, 13), value=17.16, previous_close=15.03)


def _fear_greed() -> FearGreedSnapshot:
    return FearGreedSnapshot(
        score=43.14,
        as_of=datetime(2026, 7, 14, 22, 24, 38, tzinfo=timezone.utc),
        rating="fear",
        previous_close=43.71,
        previous_1_week=40.0,
        previous_1_month=35.51,
        previous_1_year=76.11,
    )


def test_returns_200_with_both_legs_and_derived_reads():
    fake = _FakeUseCase(result=MarketSentiment(vix=_vix(), fear_greed=_fear_greed()))
    r = _client(fake).get("/market/sentiment")
    assert r.status_code == 200, r.text
    body = r.json()
    # VIX leg + derived reads
    assert body["vix"]["as_of"] == "2026-07-13"
    assert body["vix"]["value"] == 17.16
    assert body["vix"]["change"] == 2.13  # 17.16 - 15.03, entity rule via presenter
    assert body["vix"]["regime"] == "normal"
    # Fear & Greed leg + derived band/label
    assert body["fear_greed"]["score"] == 43.14
    assert body["fear_greed"]["rating"] == "fear"
    assert body["fear_greed"]["band"] == "fear"
    assert body["fear_greed"]["label"] == "Fear"
    assert body["fear_greed"]["previous_1_year"] == 76.11
    # cached for the homepage widget
    assert "max-age=900" in r.headers["cache-control"]


def test_returns_200_with_only_vix_when_fear_greed_missing():
    fake = _FakeUseCase(result=MarketSentiment(vix=_vix(), fear_greed=None))
    r = _client(fake).get("/market/sentiment")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vix"]["value"] == 17.16
    assert body["fear_greed"] is None


def test_returns_200_with_only_fear_greed_when_vix_missing():
    fake = _FakeUseCase(result=MarketSentiment(vix=None, fear_greed=_fear_greed()))
    r = _client(fake).get("/market/sentiment")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vix"] is None
    assert body["fear_greed"]["score"] == 43.14


def test_both_sources_unavailable_maps_to_502():
    fake = _FakeUseCase(error=StockDataUnavailable("*", "no sources"))
    assert _client(fake).get("/market/sentiment").status_code == 502
