"""HTTP API for reading a stock's analyst recommendation trends.

``GET /stocks/{symbol}/recommendations`` — the read endpoint for the recommendations
slice: the sell-side buy/hold/sell split by month, served from the DB cache over yfinance.
Controller + presenter + wiring, the composition-root way, sitting in
``app/stocks/endpoints/`` beside the cron entrypoint (``cron_recommendations_endpoints``)
so all of the slice's HTTP lives in one place.

Wiring convention: the process-singleton live provider is memoized with ``@lru_cache``
while the DB cache is built per request (it needs the request session). A persistent DB
cache (filled lazily on a miss, refreshed out of band by the cron endpoint) sits in front
of Yahoo so the endpoint rarely calls it — Yahoo rate-limits, so the fewer live hits the
better. yfinance needs no credential, so the endpoint is always wired (this replaced the
Finnhub source, which gated the endpoint on FINNHUB_API_KEY).
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db_cached_recommendations_adapter import (
    DbCachedRecommendationProvider,
)
from app.stocks.adapters.yfinance_recommendations_adapter import (
    YfinanceRecommendationProvider,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.recommendations.db_repository import SqlRecommendationsRepository
from app.stocks.recommendations.entities import (
    AnalystRecommendations,
    RecommendationTrend,
)
from app.stocks.recommendations.ports import RecommendationProvider
from app.stocks.recommendations.schemas import (
    RecommendationsResponse,
    RecommendationTrendResponse,
)
from app.stocks.recommendations.use_cases import GetStockRecommendations

router = APIRouter(tags=["recommendations"])


@lru_cache(maxsize=1)
def _yfinance_recommendation_provider() -> RecommendationProvider:
    # One process-singleton live provider (no key, no connection pool to share); the DB
    # cache that wraps it is built per request, since it needs the request session.
    return YfinanceRecommendationProvider()


def get_recommendation_provider(
    db: Session = Depends(get_db),
) -> RecommendationProvider:
    # A persistent DB cache (refreshed out of band by the recommendations cron endpoint +
    # lazily on a miss) sits in front of Yahoo so the endpoint rarely calls it, and it
    # serves stored rows without a live round-trip. yfinance needs no key, so this is
    # always wired.
    return DbCachedRecommendationProvider(
        _yfinance_recommendation_provider(), SqlRecommendationsRepository(db)
    )


def get_recommendations_use_case(
    provider: RecommendationProvider = Depends(get_recommendation_provider),
) -> GetStockRecommendations:
    return GetStockRecommendations(provider)


def _present_trend(trend: RecommendationTrend) -> RecommendationTrendResponse:
    return RecommendationTrendResponse(
        period=trend.period,
        strong_buy=trend.strong_buy,
        buy=trend.buy,
        hold=trend.hold,
        sell=trend.sell,
        strong_sell=trend.strong_sell,
        total=trend.total,
        score=trend.score,
        consensus=trend.consensus,
    )


def _present(recs: AnalystRecommendations) -> RecommendationsResponse:
    """Presenter: analyst-recommendations entity -> HTTP response DTO."""
    latest = recs.latest
    return RecommendationsResponse(
        symbol=recs.symbol,
        count=len(recs.trends),
        direction=recs.direction,
        latest=_present_trend(latest) if latest else None,
        trends=[_present_trend(t) for t in recs.trends],
    )


@router.get("/stocks/{symbol}/recommendations", response_model=RecommendationsResponse)
def get_stock_recommendations_endpoint(
    symbol: str,
    response: Response,
    use_case: GetStockRecommendations = Depends(get_recommendations_use_case),
) -> RecommendationsResponse:
    try:
        recs = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Analyst ratings move slowly (monthly snapshots, served from the DB cache), so cache
    # briefly: a burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(recs)
