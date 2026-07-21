from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import insider_transactions_endpoints as endpoints
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.insider_transactions.entities import (
    InsiderActivity,
    InsiderTransaction,
)


def _txn(*, code, shares, price, line_index) -> InsiderTransaction:
    return InsiderTransaction(
        filing_date=date(2026, 6, 17),
        transaction_date=date(2026, 6, 15),
        insider_name="Tim Cook",
        officer_title="Chief Executive Officer",
        is_director=False,
        is_officer=True,
        is_ten_percent_owner=False,
        security_title="Common Stock",
        transaction_code=code,
        acquired_disposed="A" if code in {"P", "M"} else "D",
        shares=shares,
        price_per_share=price,
        shares_owned_following=5000.0,
        accession_number="acc-1",
        line_index=line_index,
    )


_ACTIVITY = InsiderActivity(
    "AAPL",
    (
        _txn(code="P", shares=1000, price=200.0, line_index=0),  # buy: 200,000
        _txn(code="S", shares=500, price=210.0, line_index=1),  # sell: 105,000
        _txn(code="M", shares=300, price=None, line_index=2),  # option exercise (noise)
    ),
)


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> InsiderActivity:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_insider_transactions_use_case] = lambda: fake
    return TestClient(app)


def test_presents_the_activity_with_summary():
    resp = _client(_FakeUseCase(result=_ACTIVITY)).get("/stocks/ticker/AAPL/insider-transactions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "AAPL"
    assert body["count"] == 3
    first = body["transactions"][0]
    assert first["transaction_code"] == "P"
    assert first["code_label"] == "Open-market purchase"
    assert first["is_open_market_buy"] is True and first["is_open_market"] is True
    assert first["role"] == "Chief Executive Officer"
    assert first["value"] == 200000
    # option exercise: value null (no price), not flagged open-market
    exercise = body["transactions"][2]
    assert exercise["value"] is None and exercise["is_open_market"] is False
    summary = body["summary"]
    assert summary["open_market_buy_count"] == 1
    assert summary["open_market_sell_count"] == 1
    assert summary["open_market_buy_value"] == 200000
    assert summary["open_market_sell_value"] == 105000
    assert summary["net_value"] == 95000


def test_open_market_only_filters_transactions_but_not_the_summary():
    resp = _client(_FakeUseCase(result=_ACTIVITY)).get(
        "/stocks/ticker/AAPL/insider-transactions?open_market_only=true"
    )
    body = resp.json()
    codes = [t["transaction_code"] for t in body["transactions"]]
    assert codes == ["P", "S"]  # the M option exercise is filtered out
    assert body["count"] == 2
    # summary still reflects the full open-market rollup (unaffected by the filter)
    assert body["summary"]["net_value"] == 95000


def test_sets_the_cache_header():
    resp = _client(_FakeUseCase(result=_ACTIVITY)).get("/stocks/ticker/AAPL/insider-transactions")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_empty_activity_is_a_200_with_no_transactions():
    resp = _client(_FakeUseCase(result=InsiderActivity("ZZZZ"))).get(
        "/stocks/ticker/ZZZZ/insider-transactions"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 0 and body["transactions"] == []
    assert body["summary"]["open_market_buy_count"] == 0
    assert body["summary"]["net_value"] == 0


def test_bad_symbol_is_a_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _client(fake).get("/stocks/ticker/123/insider-transactions").status_code == 400


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ticker/ZZZZ/insider-transactions").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("AAPL", "boom"))
    assert _client(fake).get("/stocks/ticker/AAPL/insider-transactions").status_code == 502
