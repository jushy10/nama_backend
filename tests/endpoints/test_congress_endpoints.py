from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domains.ownership.congress.entities import (
    CongressActivity,
    CongressLeaderboard,
    CongressLeaderboardEntry,
    CongressMarketActivity,
    CongressTrade,
)
from app.endpoints import congress_endpoints as endpoints


def _trade(ticker="NVDA", member="Pelosi", chamber="House", tx_type="Purchase", disc=date(2026, 7, 1)):
    return CongressTrade(
        member=member,
        chamber=chamber,
        party=None,
        ticker=ticker,
        company_name=f"{ticker} Inc.",
        tx_type=tx_type,
        amount_range="$1,001 - $15,000",
        transaction_date=date(2026, 6, 20),
        disclosure_date=disc,
        owner="Self",
        source_url="http://example/1",
    )


_ACTIVITY = CongressActivity(
    "NVDA",
    (
        _trade(member="Pelosi", tx_type="Purchase"),
        _trade(member="Tuberville", chamber="Senate", tx_type="Sale"),
        _trade(member="Khanna", tx_type="Exchange"),
    ),
)


class _FakeTickerUseCase:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = []

    def execute(self, symbol):
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


class _FakeMarketUseCase:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def execute(self, *, window_days, limit, offset):
        self.calls.append((window_days, limit, offset))
        return self._result


class _FakeLeaderboardUseCase:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def execute(self, *, window_days, metric, limit):
        self.calls.append((window_days, metric, limit))
        return self._result


def _ticker_client(fake) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_congress_trades_use_case] = lambda: fake
    return TestClient(app)


def _market_client(fake) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_congress_activity_use_case] = lambda: fake
    return TestClient(app)


def _leaderboard_client(fake) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_congress_leaderboard_use_case] = lambda: fake
    return TestClient(app)


def test_per_ticker_presents_activity_with_summary():
    resp = _ticker_client(_FakeTickerUseCase(_ACTIVITY)).get("/stocks/ticker/NVDA/congress-trades")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "NVDA"
    assert body["total"] == 3 and body["count"] == 3
    first = body["items"][0]
    assert first["member"] == "Pelosi" and first["chamber"] == "House"
    assert first["is_buy"] is True and first["is_sell"] is False
    assert first["name"] == "NVDA Inc." and first["amount_midpoint"] == 8000.5
    # Summary: 1 buy, 1 sell (the exchange counts toward neither).
    summary = body["summary"]
    assert summary["buy_count"] == 1 and summary["sell_count"] == 1
    assert summary["net_value"] == 8000.5 - 8000.5


def test_per_ticker_paginates_but_summary_reflects_full_set():
    resp = _ticker_client(_FakeTickerUseCase(_ACTIVITY)).get(
        "/stocks/ticker/NVDA/congress-trades?limit=1&offset=1"
    )
    body = resp.json()
    assert body["total"] == 3 and body["count"] == 1
    assert body["items"][0]["member"] == "Tuberville"  # the second trade
    # Summary is over all 3, not the single page.
    assert body["summary"]["buy_count"] == 1 and body["summary"]["sell_count"] == 1


def test_per_ticker_sets_cache_header():
    resp = _ticker_client(_FakeTickerUseCase(_ACTIVITY)).get("/stocks/ticker/NVDA/congress-trades")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_per_ticker_empty_is_a_200():
    resp = _ticker_client(_FakeTickerUseCase(CongressActivity("ZZZZ"))).get(
        "/stocks/ticker/ZZZZ/congress-trades"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0 and body["items"] == []
    assert body["summary"]["net_value"] == 0


def test_per_ticker_bad_symbol_is_a_400():
    fake = _FakeTickerUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _ticker_client(fake).get("/stocks/ticker/123/congress-trades").status_code == 400


def test_per_ticker_rejects_out_of_range_limit():
    resp = _ticker_client(_FakeTickerUseCase(_ACTIVITY)).get(
        "/stocks/ticker/NVDA/congress-trades?limit=0"
    )
    assert resp.status_code == 422


def test_market_presents_the_board():
    result = CongressMarketActivity(
        trades=(_trade(member="Pelosi"), _trade(ticker="AAPL", member="Khanna", tx_type="Sale")),
        total=42,
        window_days=30,
    )
    fake = _FakeMarketUseCase(result)
    resp = _market_client(fake).get("/market/congress-activity?window=30d&limit=10&offset=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["window"] == "30d" and body["total"] == 42 and body["count"] == 2
    assert [i["ticker"] for i in body["items"]] == ["NVDA", "AAPL"]
    # The controller resolved 30d -> 30 days and passed it to the use case.
    assert fake.calls == [(30, 10, 0)]


def test_market_defaults_to_30d():
    fake = _FakeMarketUseCase(CongressMarketActivity((), 0, 30))
    resp = _market_client(fake).get("/market/congress-activity")
    assert resp.status_code == 200
    assert resp.json()["window"] == "30d"
    assert fake.calls[0][0] == 30


def test_market_all_window_passes_none_days():
    fake = _FakeMarketUseCase(CongressMarketActivity((), 0, None))
    _market_client(fake).get("/market/congress-activity?window=all")
    assert fake.calls[0][0] is None


def test_market_bad_window_is_a_400():
    fake = _FakeMarketUseCase(CongressMarketActivity((), 0, 30))
    resp = _market_client(fake).get("/market/congress-activity?window=bogus")
    assert resp.status_code == 400
    assert fake.calls == []  # the controller rejected before calling the use case


def test_market_empty_is_a_200():
    fake = _FakeMarketUseCase(CongressMarketActivity((), 0, 30))
    resp = _market_client(fake).get("/market/congress-activity")
    assert resp.status_code == 200
    assert resp.json()["items"] == [] and resp.json()["total"] == 0


def _entry(ticker="NVDA", members=3, trades=4, buy=3, sell=1, buy_value=24000.0, sell_value=8000.0):
    return CongressLeaderboardEntry(
        ticker=ticker,
        company_name=f"{ticker} Inc.",
        trade_count=trades,
        member_count=members,
        buy_count=buy,
        sell_count=sell,
        buy_value=buy_value,
        sell_value=sell_value,
        last_activity=date(2026, 7, 1),
    )


def test_leaderboard_presents_the_ranked_board():
    board = CongressLeaderboard(
        entries=(_entry("NVDA", members=3), _entry("AAPL", members=2)),
        metric="members",
        window_days=30,
        total_stocks=17,
    )
    fake = _FakeLeaderboardUseCase(board)
    resp = _leaderboard_client(fake).get(
        "/market/congress-leaderboard?window=30d&metric=members&limit=10"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["window"] == "30d" and body["metric"] == "members"
    assert body["total"] == 17 and body["count"] == 2
    assert [i["ticker"] for i in body["items"]] == ["NVDA", "AAPL"]
    first = body["items"][0]
    assert first["name"] == "NVDA Inc." and first["member_count"] == 3
    # net_value / total_value are surfaced derived (buy - sell / buy + sell).
    assert first["net_value"] == 16000.0 and first["total_value"] == 32000.0
    # The controller resolved 30d -> 30 days and passed the metric through.
    assert fake.calls == [(30, "members", 10)]


def test_leaderboard_defaults_window_and_metric():
    fake = _FakeLeaderboardUseCase(CongressLeaderboard((), "members", 30, 0))
    resp = _leaderboard_client(fake).get("/market/congress-leaderboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"] == "30d" and body["metric"] == "members"
    assert fake.calls[0] == (30, "members", 20)  # default limit 20


def test_leaderboard_bad_window_is_a_400():
    fake = _FakeLeaderboardUseCase(CongressLeaderboard((), "members", 30, 0))
    resp = _leaderboard_client(fake).get("/market/congress-leaderboard?window=bogus")
    assert resp.status_code == 400
    assert fake.calls == []  # rejected before calling the use case


def test_leaderboard_bad_metric_is_a_400():
    fake = _FakeLeaderboardUseCase(CongressLeaderboard((), "members", 30, 0))
    resp = _leaderboard_client(fake).get("/market/congress-leaderboard?metric=bogus")
    assert resp.status_code == 400
    assert fake.calls == []


def test_leaderboard_sets_cache_header():
    fake = _FakeLeaderboardUseCase(CongressLeaderboard((_entry(),), "members", 30, 1))
    resp = _leaderboard_client(fake).get("/market/congress-leaderboard")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_leaderboard_rejects_out_of_range_limit():
    fake = _FakeLeaderboardUseCase(CongressLeaderboard((), "members", 30, 0))
    resp = _leaderboard_client(fake).get("/market/congress-leaderboard?limit=0")
    assert resp.status_code == 422


def test_leaderboard_empty_is_a_200():
    fake = _FakeLeaderboardUseCase(CongressLeaderboard((), "members", 30, 0))
    resp = _leaderboard_client(fake).get("/market/congress-leaderboard")
    assert resp.status_code == 200
    assert resp.json()["items"] == [] and resp.json()["total"] == 0
