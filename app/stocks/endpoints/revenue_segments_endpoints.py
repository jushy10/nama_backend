from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db_cached_revenue_segments_adapter import (
    DbCachedRevenueSegmentsProvider,
)
from app.stocks.adapters.sec_edgar_revenue_segments_adapter import (
    SecEdgarRevenueSegmentsProvider,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.revenue_segments.db_repository import SqlRevenueSegmentsRepository
from app.stocks.revenue_segments.entities import (
    RevenueSegment,
    RevenueSegmentation,
)
from app.stocks.revenue_segments.ports import RevenueSegmentsProvider
from app.stocks.revenue_segments.schemas import (
    RevenueSegmentationResponse,
    RevenueSegmentResponse,
)
from app.stocks.revenue_segments.use_cases import GetRevenueSegments

router = APIRouter(tags=["revenue-segments"])

# Production pacing for the live SEC provider: the read path only fetches on a cold miss (a few
# sequential EDGAR requests), so a small per-request spacing keeps even a burst of cold misses
# under EDGAR's ~10 req/s fair-use ceiling.
_SEC_MIN_REQUEST_INTERVAL = 0.15


@lru_cache(maxsize=1)
def _sec_revenue_segments_provider() -> RevenueSegmentsProvider:
    # One process-singleton live provider (no key; it caches the ticker->CIK map across calls);
    # the DB cache that wraps it is built per request, since it needs the request session.
    return SecEdgarRevenueSegmentsProvider(
        min_request_interval_seconds=_SEC_MIN_REQUEST_INTERVAL
    )


def get_revenue_segments_provider(
    db: Session = Depends(get_db),
) -> RevenueSegmentsProvider:
    # A persistent DB cache (refreshed out of band by the revenue-segments cron endpoint + lazily
    # on a miss) sits in front of EDGAR so the endpoint rarely walks the filing. SEC needs no
    # key, so this is always wired.
    return DbCachedRevenueSegmentsProvider(
        _sec_revenue_segments_provider(), SqlRevenueSegmentsRepository(db)
    )


def get_revenue_segments_use_case(
    provider: RevenueSegmentsProvider = Depends(get_revenue_segments_provider),
) -> GetRevenueSegments:
    return GetRevenueSegments(provider)


def _present_segment(segment: RevenueSegment) -> RevenueSegmentResponse:
    return RevenueSegmentResponse(
        fiscal_year=segment.fiscal_year,
        period_end=segment.period_end,
        axis=segment.axis.value,
        member=segment.member,
        label=segment.label,
        value=segment.value,
    )


def _present(segmentation: RevenueSegmentation) -> RevenueSegmentationResponse:
    return RevenueSegmentationResponse(
        symbol=segmentation.symbol,
        count=len(segmentation.segments),
        fiscal_years=list(segmentation.fiscal_years),
        latest_fiscal_year=segmentation.latest_fiscal_year,
        segments=[_present_segment(s) for s in segmentation.segments],
    )


@router.get(
    "/stocks/{symbol}/revenue-segments",
    response_model=RevenueSegmentationResponse,
)
def get_revenue_segments_endpoint(
    symbol: str,
    response: Response,
    use_case: GetRevenueSegments = Depends(get_revenue_segments_use_case),
) -> RevenueSegmentationResponse:
    try:
        segmentation = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Segment data moves once a year (on a filing) and is served from the DB cache, so cache
    # briefly: a burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(segmentation)
