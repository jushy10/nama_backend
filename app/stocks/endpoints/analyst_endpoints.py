"""HTTP API for reading a stock's full analyst coverage in one payload.

``GET /stocks/ticker/{ticker}/analyst-info`` — the read endpoint for the analyst-info card:
the sell-side recommendation trends (buy/hold/sell by month), the current consensus price
target, and the discrete upgrade/downgrade events, all served from the DB cache over
yfinance. It consolidates what the two older reads served separately —
``/stocks/{symbol}/recommendations`` (trends + targets) and ``/stocks/{symbol}/rating-changes``
(the events) — into the single payload the frontend's Analysts view consumes. Controller +
presenter + wiring, the composition-root way, sitting in ``app/stocks/endpoints/`` beside the
slice's cron entrypoint (``cron_recommendations_endpoints``) so all of the slice's HTTP lives
in one place. (The path is grouped under the ``/stocks/ticker/{ticker}`` resource — the same
prefix the ticker card and its ``pe-history`` sub-read use — because it's a per-ticker card the
FE renders on the stock screen; the data still comes from this slice.)

Primary vs. enrichment: the recommendation trends are primary (their failure maps to a 404/502
like the old standalone read), the rating-change events are best-effort enrichment folded in by
the use case (``GetStockAnalystInfo``), so a rating-change outage can never sink the trends.

Wiring convention: the process-singleton live providers are memoized with ``@lru_cache`` while
the DB caches are built per request (they need the request session). A persistent DB cache in
front of each source — filled lazily on a cold miss, refreshed out of band by the recommendations
cron, which folds the rating-change refresh into its sweep — keeps the endpoint off Yahoo, which
rate-limits data-centre IPs. yfinance needs no credential, so the endpoint is always wired.
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db_cached_rating_changes_adapter import (
    DbCachedRatingChangeProvider,
)
from app.stocks.adapters.db_cached_recommendations_adapter import (
    DbCachedRecommendationProvider,
)
from app.stocks.adapters.yfinance_rating_changes_adapter import (
    YfinanceRatingChangeProvider,
)
from app.stocks.adapters.yfinance_recommendations_adapter import (
    YfinanceRecommendationProvider,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.recommendations.db_repository import (
    SqlRatingChangesRepository,
    SqlRecommendationsRepository,
)
from app.stocks.recommendations.entities import (
    AnalystPriceTargets,
    AnalystRecommendations,
    FirmRating,
    RatingChange,
    RecommendationTrend,
)
from app.stocks.recommendations.ports import (
    RatingChangeProvider,
    RecommendationProvider,
)
from app.stocks.recommendations.schemas import (
    AnalystInfoResponse,
    AnalystPriceTargetsResponse,
    AnalystRecommendationsBlock,
    RatingChangeResponse,
    RecommendationTrendResponse,
    TopFirmRatingResponse,
)
from app.stocks.recommendations.use_cases import AnalystInfo, GetStockAnalystInfo

router = APIRouter(tags=["analyst-info"])


@lru_cache(maxsize=1)
def _yfinance_recommendation_provider() -> RecommendationProvider:
    # One process-singleton live provider (no key, no connection pool to share); the DB
    # cache that wraps it is built per request, since it needs the request session.
    return YfinanceRecommendationProvider()


@lru_cache(maxsize=1)
def _yfinance_rating_change_provider() -> RatingChangeProvider:
    # Its rating-change sibling — same singleton rationale.
    return YfinanceRatingChangeProvider()


def get_recommendation_provider(
    db: Session = Depends(get_db),
) -> RecommendationProvider:
    # A persistent DB cache (refreshed out of band by the recommendations cron + lazily on a
    # miss) sits in front of Yahoo so the endpoint rarely calls it, and it serves stored rows
    # without a live round-trip. yfinance needs no key, so this is always wired.
    return DbCachedRecommendationProvider(
        _yfinance_recommendation_provider(), SqlRecommendationsRepository(db)
    )


def get_rating_change_provider(
    db: Session = Depends(get_db),
) -> RatingChangeProvider:
    # The rating-change DB cache, wired the same way (refreshed by the same sweep + lazily on
    # a miss). Keyless, so always wired.
    return DbCachedRatingChangeProvider(
        _yfinance_rating_change_provider(), SqlRatingChangesRepository(db)
    )


def get_analyst_info_use_case(
    recommendations: RecommendationProvider = Depends(get_recommendation_provider),
    rating_changes: RatingChangeProvider = Depends(get_rating_change_provider),
) -> GetStockAnalystInfo:
    return GetStockAnalystInfo(recommendations, rating_changes)


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


def _present_targets(targets: AnalystPriceTargets) -> AnalystPriceTargetsResponse:
    return AnalystPriceTargetsResponse(
        mean=targets.mean,
        high=targets.high,
        low=targets.low,
        median=targets.median,
    )


def _present_change(change: RatingChange) -> RatingChangeResponse:
    return RatingChangeResponse(
        firm=change.firm,
        published_at=change.published_at,
        action=change.action,
        from_grade=change.from_grade,
        to_grade=change.to_grade,
        target_current=change.target_current,
        target_prior=change.target_prior,
        is_upgrade=change.is_upgrade,
        is_downgrade=change.is_downgrade,
    )


def _present_top_firm(firm: FirmRating) -> TopFirmRatingResponse:
    return TopFirmRatingResponse(
        firm=firm.firm,
        rank=firm.rank,
        rating=firm.rating,
        action=firm.action,
        target=firm.target,
        published_at=firm.published_at,
    )


def _present_recommendations(
    recs: AnalystRecommendations,
) -> AnalystRecommendationsBlock:
    latest = recs.latest
    targets = recs.price_targets
    return AnalystRecommendationsBlock(
        direction=recs.direction,
        latest=_present_trend(latest) if latest else None,
        price_targets=_present_targets(targets) if targets else None,
        trends=[_present_trend(t) for t in recs.trends],
    )


def _present(info: AnalystInfo) -> AnalystInfoResponse:
    """Presenter: analyst-info composite -> HTTP response DTO. The domain speaks in
    ``symbol``; renaming it ``ticker`` is a JSON-shape choice made here at the edge, like
    the ticker card's."""
    return AnalystInfoResponse(
        ticker=info.symbol,
        recommendations=_present_recommendations(info.recommendations),
        rating_changes=[_present_change(c) for c in info.rating_changes.changes],
        top_firms=[_present_top_firm(f) for f in info.top_firms],
    )


@router.get(
    "/stocks/ticker/{ticker}/analyst-info", response_model=AnalystInfoResponse
)
def get_stock_analyst_info_endpoint(
    ticker: str,
    response: Response,
    use_case: GetStockAnalystInfo = Depends(get_analyst_info_use_case),
) -> AnalystInfoResponse:
    try:
        info = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # The trends are primary, so their StockNotFound/StockDataUnavailable propagate here; the
    # rating-change leg is best-effort inside the use case and can't reach this mapping.
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Analyst coverage moves slowly (monthly snapshots + accreting events, served from the DB
    # cache), so cache briefly: a burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(info)
