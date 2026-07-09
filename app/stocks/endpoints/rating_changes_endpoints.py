"""HTTP API for reading a stock's analyst rating actions (upgrades/downgrades).

``GET /stocks/{symbol}/rating-changes`` — the read endpoint for the recommendations slice's
discrete-event feed: the sell-side's individual upgrade/downgrade actions (firm, date,
from/to grade, action, old/new price target), newest first, served from the DB cache over
yfinance. The read counterpart of the events the recommendations sweep already stores; it
sits in ``app/stocks/endpoints/`` beside the trends read (``recommendations_endpoints``) so
all of the slice's HTTP lives in one place.

Wiring convention (identical to the trends read): the process-singleton live provider is
memoized with ``@lru_cache`` while the DB cache is built per request (it needs the request
session). A persistent DB cache — filled lazily on a cold miss, and kept current out of band
by the recommendations cron, which folds the rating-change refresh into its sweep — sits in
front of Yahoo so the endpoint rarely calls it. yfinance needs no credential, so the endpoint
is always wired.
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db_cached_rating_changes_adapter import (
    DbCachedRatingChangeProvider,
)
from app.stocks.adapters.yfinance_rating_changes_adapter import (
    YfinanceRatingChangeProvider,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.recommendations.db_repository import SqlRatingChangesRepository
from app.stocks.recommendations.entities import AnalystRatingChanges, RatingChange
from app.stocks.recommendations.ports import RatingChangeProvider
from app.stocks.recommendations.schemas import (
    RatingChangeResponse,
    RatingChangesResponse,
)
from app.stocks.recommendations.use_cases import GetStockRatingChanges

router = APIRouter(tags=["rating-changes"])


@lru_cache(maxsize=1)
def _yfinance_rating_change_provider() -> RatingChangeProvider:
    # One process-singleton live provider (no key, no connection pool to share); the DB cache
    # that wraps it is built per request, since it needs the request session.
    return YfinanceRatingChangeProvider()


def get_rating_change_provider(
    db: Session = Depends(get_db),
) -> RatingChangeProvider:
    # A persistent DB cache (refreshed out of band by the recommendations cron + lazily on a
    # miss) sits in front of Yahoo so the endpoint rarely calls it. yfinance needs no key, so
    # this is always wired.
    return DbCachedRatingChangeProvider(
        _yfinance_rating_change_provider(), SqlRatingChangesRepository(db)
    )


def get_rating_changes_use_case(
    provider: RatingChangeProvider = Depends(get_rating_change_provider),
) -> GetStockRatingChanges:
    return GetStockRatingChanges(provider)


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


def _present(rating_changes: AnalystRatingChanges) -> RatingChangesResponse:
    """Presenter: analyst-rating-changes entity -> HTTP response DTO."""
    return RatingChangesResponse(
        symbol=rating_changes.symbol,
        count=len(rating_changes.changes),
        changes=[_present_change(c) for c in rating_changes.changes],
    )


@router.get("/stocks/{symbol}/rating-changes", response_model=RatingChangesResponse)
def get_stock_rating_changes_endpoint(
    symbol: str,
    response: Response,
    use_case: GetStockRatingChanges = Depends(get_rating_changes_use_case),
) -> RatingChangesResponse:
    try:
        rating_changes = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Rating actions accrue slowly and are served from the DB cache, so cache briefly: a burst
    # of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(rating_changes)
