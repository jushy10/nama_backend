from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.endpoints import institutional_ownership_endpoints as endpoints
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.ownership.institutional_ownership.entities import (
    HOLDER_TYPE_INSTITUTION,
    HOLDER_TYPE_MUTUAL_FUND,
    InstitutionalHolder,
    InstitutionalOwnership,
    OwnershipBreakdown,
)


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> InstitutionalOwnership:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_institutional_ownership_use_case] = lambda: fake
    return TestClient(app)


def _holder(holder, *, holder_type=HOLDER_TYPE_INSTITUTION, reported=date(2026, 6, 30), pct_change=None, shares=1000.0, value=100000.0):
    return InstitutionalHolder(
        holder=holder,
        holder_type=holder_type,
        date_reported=reported,
        shares=shares,
        value=value,
        pct_held=8.9,
        pct_change=pct_change,
    )


def test_presents_holders_breakdown_and_flow():
    ownership = InstitutionalOwnership(
        symbol="AAPL",
        breakdown=OwnershipBreakdown(62.3, 0.07, 63.0, 5321),
        holders=(
            _holder("Buyer", shares=1100.0, value=110000.0, pct_change=10.0),
            _holder("Seller", holder_type=HOLDER_TYPE_MUTUAL_FUND, shares=900.0, value=90000.0, pct_change=-10.0),
        ),
    )
    resp = _client(_FakeUseCase(result=ownership)).get(
        "/stocks/ticker/AAPL/institutional-ownership"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "AAPL"
    assert body["count"] == 2
    assert body["latest_report_date"] == "2026-06-30"
    assert body["breakdown"]["institutions_pct_held"] == 62.3
    assert body["breakdown"]["institutions_count"] == 5321
    # Derived per-holder flags + change surfaced.
    buyer = body["holders"][0]
    assert buyer["is_buyer"] is True and buyer["is_seller"] is False
    assert buyer["share_change"] == pytest.approx(100.0)
    assert body["holders"][1]["holder_type"] == "mutual_fund"
    # Flow rolls up the latest snapshot.
    flow = body["flow"]
    assert (flow["buyers_count"], flow["sellers_count"]) == (1, 1)
    assert flow["shares_bought"] == pytest.approx(100.0)
    assert flow["shares_sold"] == pytest.approx(100.0)
    assert flow["net_value_change"] == pytest.approx(0.0, abs=1e-6)


def test_sets_the_cache_header():
    ownership = InstitutionalOwnership(symbol="AAPL", holders=(_holder("V"),))
    resp = _client(_FakeUseCase(result=ownership)).get(
        "/stocks/ticker/AAPL/institutional-ownership"
    )
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_empty_coverage_is_a_200_with_no_holders():
    resp = _client(_FakeUseCase(result=InstitutionalOwnership(symbol="ZZZZ"))).get(
        "/stocks/ticker/ZZZZ/institutional-ownership"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 0
    assert body["holders"] == []
    assert body["breakdown"] is None
    assert body["latest_report_date"] is None
    assert body["flow"]["buyers_count"] == 0  # a zeroed flow, not null


def test_bad_symbol_is_a_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _client(fake).get("/stocks/ticker/123/institutional-ownership").status_code == 400


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ticker/ZZZZ/institutional-ownership").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("AAPL", "boom"))
    assert _client(fake).get("/stocks/ticker/AAPL/institutional-ownership").status_code == 502
