from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import options_endpoints as endpoints
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.options.entities import ExpiryChain, OptionContract, OptionType
from app.stocks.options.use_cases import OptionsFlow

_EXPIRY = date(2026, 7, 31)
_FAR = date(2026, 9, 18)


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, date | None]] = []

    def execute(self, symbol, expiration=None):
        self.calls.append((symbol, expiration))
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_options_flow_use_case] = lambda: fake
    return TestClient(app)


def _c(strike, option_type=OptionType.CALL, **kw) -> OptionContract:
    return OptionContract(expiration=_EXPIRY, strike=strike, option_type=option_type, **kw)


def _a_flow() -> OptionsFlow:
    call = _c(100, OptionType.CALL, bid=2.8, ask=3.2, volume=500, open_interest=100,
              implied_volatility=0.25, in_the_money=True)  # unusual (500 > 100)
    put = _c(95, OptionType.PUT, bid=1.9, ask=2.1, volume=100, open_interest=900,
             implied_volatility=0.30, in_the_money=False)  # not unusual
    chain = ExpiryChain(expiration=_EXPIRY, spot=101.5, contracts=(call, put))
    return OptionsFlow(symbol="AAPL", expirations=(_EXPIRY, _FAR), chain=chain)


def test_returns_the_chain_summary_and_unusual_shape():
    fake = _FakeUseCase(result=_a_flow())
    resp = _client(fake).get("/stocks/ticker/AAPL/options")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert body["spot"] == 101.5
    assert body["expiration"] == "2026-07-31"
    assert body["expirations"] == ["2026-07-31", "2026-09-18"]

    (call,) = body["calls"]
    assert call["type"] == "call"
    assert call["mid"] == 3.0
    assert call["implied_volatility"] == 25.0  # decimal fraction rendered as percent
    assert call["premium"] == 3.0 * 500 * 100  # mid × volume × lot
    assert call["unusual"] is True
    assert call["in_the_money"] is True

    (put,) = body["puts"]
    assert put["type"] == "put" and put["unusual"] is False

    # Aggregates + the unusual highlight (only the call qualifies).
    assert body["summary"]["call_volume"] == 500
    assert body["summary"]["put_call_volume_ratio"] == 0.2  # 100 / 500
    assert [u["strike"] for u in body["unusual"]] == [100.0]

    assert resp.headers["cache-control"] == "public, max-age=120"


def test_expiration_query_is_passed_through():
    fake = _FakeUseCase(result=_a_flow())
    _client(fake).get("/stocks/ticker/AAPL/options", params={"expiration": "2026-09-18"})
    assert fake.calls == [("AAPL", date(2026, 9, 18))]


def test_unusual_list_is_capped():
    many = tuple(
        _c(100 + i, OptionType.CALL, bid=1.0, ask=1.2, volume=1000, open_interest=1)
        for i in range(40)
    )  # all unusual
    chain = ExpiryChain(expiration=_EXPIRY, spot=120.0, contracts=many)
    fake = _FakeUseCase(result=OptionsFlow(symbol="AAPL", expirations=(_EXPIRY,), chain=chain))
    body = _client(fake).get("/stocks/ticker/AAPL/options").json()
    assert len(body["unusual"]) == endpoints._MAX_UNUSUAL
    assert len(body["calls"]) == 40  # the full chain is not capped


def test_no_listed_options_is_an_empty_flow_not_a_404():
    fake = _FakeUseCase(result=OptionsFlow(symbol="ZZZZ", expirations=(), chain=None))
    resp = _client(fake).get("/stocks/ticker/ZZZZ/options")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "ZZZZ"
    assert body["expiration"] is None
    assert body["summary"] is None
    assert body["calls"] == [] and body["puts"] == [] and body["expirations"] == []


@pytest.mark.parametrize(
    "error, status",
    [
        (ValueError("bad"), 400),
        (StockNotFound("AAPL"), 404),
        (StockDataUnavailable("AAPL", "blocked"), 502),
    ],
)
def test_error_mapping(error, status):
    fake = _FakeUseCase(error=error)
    assert _client(fake).get("/stocks/ticker/AAPL/options").status_code == status
