from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import analyst_endpoints as endpoints
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.recommendations.entities import (
    AnalystPriceTargets,
    AnalystRatingChanges,
    AnalystRecommendations,
    FirmRating,
    RatingChange,
    RecommendationTrend,
)
from app.stocks.recommendations.use_cases import AnalystInfo


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> AnalystInfo:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_analyst_info_use_case] = lambda: fake
    return TestClient(app)


def _a_trend(period, *, strong_buy=0, buy=0, hold=0, sell=0, strong_sell=0):
    return RecommendationTrend(
        period=period,
        strong_buy=strong_buy,
        buy=buy,
        hold=hold,
        sell=sell,
        strong_sell=strong_sell,
    )


def _an_info(
    symbol="AAPL", *, trends=(), price_targets=None, changes=(), top_firms=()
) -> AnalystInfo:
    return AnalystInfo(
        symbol=symbol,
        recommendations=AnalystRecommendations(symbol, tuple(trends), price_targets),
        rating_changes=AnalystRatingChanges(symbol, tuple(changes)),
        top_firms=tuple(top_firms),
    )


def test_presents_recommendations_block_with_consensus_and_direction():
    info = _an_info(
        trends=(
            _a_trend(date(2026, 6, 1), strong_buy=13, buy=24, hold=7),  # mean 1.86
            _a_trend(date(2026, 5, 1), strong_buy=10, buy=20, hold=10, sell=1),  # 2.05
        ),
    )
    resp = _client(_FakeUseCase(result=info)).get("/stocks/ticker/AAPL/analyst-info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticker"] == "AAPL"
    recs = body["recommendations"]
    assert recs["direction"] == "upgraded"  # consensus got more bullish MoM
    assert recs["latest"]["consensus"] == "Buy"
    assert recs["latest"]["score"] == 1.86
    assert recs["latest"]["total"] == 44
    assert len(recs["trends"]) == 2


def test_presents_the_price_targets_block():
    info = _an_info(
        trends=(_a_trend(date(2026, 6, 1), buy=5),),
        price_targets=AnalystPriceTargets(mean=315.5, high=400.0, low=215.0, median=315.0),
    )
    body = _client(_FakeUseCase(result=info)).get("/stocks/ticker/AAPL/analyst-info").json()
    assert body["recommendations"]["price_targets"] == {
        "mean": 315.5,
        "high": 400.0,
        "low": 215.0,
        "median": 315.0,
    }


def test_price_targets_is_null_when_absent():
    info = _an_info(trends=(_a_trend(date(2026, 6, 1), buy=5),))
    body = _client(_FakeUseCase(result=info)).get("/stocks/ticker/AAPL/analyst-info").json()
    assert body["recommendations"]["price_targets"] is None


def test_presents_rating_changes_with_derived_direction_flags():
    info = _an_info(
        trends=(_a_trend(date(2026, 6, 1), buy=5),),
        changes=(
            RatingChange(
                "TD Cowen",
                date(2026, 6, 9),
                action="up",
                from_grade="Hold",
                to_grade="Buy",
                target_current=350.0,
                target_prior=335.0,
            ),
            RatingChange("KGI Securities", date(2026, 5, 1), action="down", to_grade="Hold"),
        ),
    )
    body = _client(_FakeUseCase(result=info)).get("/stocks/ticker/AAPL/analyst-info").json()
    changes = body["rating_changes"]
    assert len(changes) == 2
    first = changes[0]
    assert first["firm"] == "TD Cowen"
    assert first["from_grade"] == "Hold" and first["to_grade"] == "Buy"
    assert first["target_current"] == 350.0 and first["target_prior"] == 335.0
    assert first["is_upgrade"] is True and first["is_downgrade"] is False
    assert changes[1]["is_downgrade"] is True


def test_presents_the_top_firms_block():
    info = _an_info(
        trends=(_a_trend(date(2026, 6, 1), buy=5),),
        top_firms=(
            FirmRating(
                firm="RBC Capital",
                rank=1,
                rating="Outperform",
                action="main",
                target=270.0,
                published_at=date(2026, 5, 21),
            ),
            FirmRating(
                firm="Evercore ISI Group",
                rank=2,
                rating="Outperform",
                action="main",
                target=413.0,
                published_at=date(2026, 5, 21),
            ),
        ),
    )
    body = _client(_FakeUseCase(result=info)).get("/stocks/ticker/AAPL/analyst-info").json()
    top = body["top_firms"]
    assert len(top) == 2
    assert top[0] == {
        "firm": "RBC Capital",
        "rank": 1,
        "rating": "Outperform",
        "action": "main",
        "target": 270.0,
        "published_at": "2026-05-21",
    }
    assert top[1]["firm"] == "Evercore ISI Group"


def test_top_firms_is_an_empty_list_when_none():
    info = _an_info(trends=(_a_trend(date(2026, 6, 1), buy=5),))
    body = _client(_FakeUseCase(result=info)).get("/stocks/ticker/AAPL/analyst-info").json()
    assert body["top_firms"] == []


def test_sets_the_cache_header():
    info = _an_info(trends=(_a_trend(date(2026, 6, 1), buy=5),))
    resp = _client(_FakeUseCase(result=info)).get("/stocks/ticker/AAPL/analyst-info")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_empty_coverage_is_a_200_with_empty_blocks():
    resp = _client(_FakeUseCase(result=_an_info("ZZZZ"))).get(
        "/stocks/ticker/ZZZZ/analyst-info"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    recs = body["recommendations"]
    assert recs["latest"] is None
    assert recs["direction"] is None
    assert recs["price_targets"] is None
    assert recs["trends"] == []
    assert body["rating_changes"] == []


def test_forwards_the_ticker_to_the_use_case():
    fake = _FakeUseCase(result=_an_info())
    _client(fake).get("/stocks/ticker/aapl/analyst-info")
    assert fake.calls == ["aapl"]  # normalization is the use case's job, not the controller's


def test_bad_symbol_is_a_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _client(fake).get("/stocks/ticker/123/analyst-info").status_code == 400


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ticker/ZZZZ/analyst-info").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("AAPL", "boom"))
    assert _client(fake).get("/stocks/ticker/AAPL/analyst-info").status_code == 502
