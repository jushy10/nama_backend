from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.endpoints import yields_endpoints as endpoints
from app.endpoints.error_handlers import register_error_handlers
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.macro.yields.entities import (
    YieldCurve,
    YieldHistory,
    YieldObservation,
    YieldSeries,
    YieldTenor,
)


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list = []

    def run(self, *args) -> object:
        self.calls.append(args)
        if self._error is not None:
            raise self._error
        return self._result


def _curve_client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    register_error_handlers(app)  # the endpoint has no try/except; the handlers translate
    app.dependency_overrides[endpoints.get_get_yield_curve] = lambda: fake
    return TestClient(app)


def _history_client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    register_error_handlers(app)
    app.dependency_overrides[endpoints.get_get_yield_history] = lambda: fake
    return TestClient(app)


def _a_curve() -> YieldCurve:
    return YieldCurve(
        as_of=date(2026, 7, 13),
        tenors=(
            YieldTenor(label="2Y", months=24.0, rate=4.26),
            YieldTenor(label="3M", months=3.0, rate=3.89),
            YieldTenor(label="10Y", months=120.0, rate=4.62),
        ),
    )


def test_get_yield_curve_returns_200_with_derived_reads():
    client = _curve_client(_FakeUseCase(result=_a_curve()))
    r = client.get("/market/yield-curve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["as_of"] == "2026-07-13"
    assert body["count"] == 3
    assert body["two_year"] == 4.26
    assert body["ten_year"] == 4.62
    assert body["spread_2s10s"] == 0.36  # 10Y - 2Y, entity rule via presenter
    assert body["is_inverted"] is False


def test_get_yield_curve_preserves_provider_order_in_tenors():
    # The presenter maps 1:1; the use case owns ordering, so the fake's order rides through.
    client = _curve_client(_FakeUseCase(result=_a_curve()))
    labels = [t["label"] for t in client.get("/market/yield-curve").json()["tenors"]]
    assert labels == ["2Y", "3M", "10Y"]


def test_get_yield_curve_upstream_failure_502():
    fake = _FakeUseCase(error=StockDataUnavailable("*", "boom"))
    assert _curve_client(fake).get("/market/yield-curve").status_code == 502


def test_get_yield_curve_not_found_404():
    fake = _FakeUseCase(error=StockNotFound("*"))
    assert _curve_client(fake).get("/market/yield-curve").status_code == 404


def _a_history() -> YieldHistory:
    return YieldHistory(
        series=(
            YieldSeries(
                label="2Y",
                observations=(
                    YieldObservation(on=date(2026, 7, 1), rate=4.20),
                    YieldObservation(on=date(2026, 7, 2), rate=4.26),
                ),
            ),
            YieldSeries(
                label="10Y",
                observations=(
                    YieldObservation(on=date(2026, 7, 1), rate=4.55),
                    YieldObservation(on=date(2026, 7, 2), rate=4.62),
                ),
            ),
        )
    )


def test_get_yield_history_returns_200_with_series_and_spread():
    client = _history_client(_FakeUseCase(result=_a_history()))
    r = client.get("/market/yield-history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["label"] for s in body["series"]] == ["2Y", "10Y"]
    assert body["series"][0]["observations"][0] == {"date": "2026-07-01", "rate": 4.2}
    # spread series is derived on shared dates (10Y - 2Y)
    assert body["spread"] == [
        {"date": "2026-07-01", "rate": 0.35},
        {"date": "2026-07-02", "rate": 0.36},
    ]
    assert body["latest_spread"] == 0.36
    assert body["is_inverted"] is False


def test_get_yield_history_forwards_lookback_days_to_use_case():
    fake = _FakeUseCase(result=_a_history())
    _history_client(fake).get("/market/yield-history?lookback_days=30")
    assert fake.calls == [(30,)]


def test_get_yield_history_rejects_bad_lookback_400():
    # Query validation (ge=1) fires before the use case; FastAPI returns 422.
    client = _history_client(_FakeUseCase(result=_a_history()))
    assert client.get("/market/yield-history?lookback_days=0").status_code == 422


def test_get_yield_history_upstream_failure_502():
    fake = _FakeUseCase(error=StockDataUnavailable("*", "boom"))
    assert _history_client(fake).get("/market/yield-history").status_code == 502
