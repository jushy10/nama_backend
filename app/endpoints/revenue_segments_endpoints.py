from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.domains.financials.revenue_segments import wiring
from app.domains.financials.revenue_segments.api_schemas import (
    RevenueSegmentationResponse,
)
from app.domains.financials.revenue_segments.use_cases import GetRevenueSegments

router = APIRouter(tags=["revenue-segments"])


def get_get_revenue_segments(db: Session = Depends(get_db)) -> GetRevenueSegments:
    # Depends shim over the slice's wiring — exists for the db lifecycle and the
    # dependency_overrides test seam, nothing more.
    return wiring.build_get_revenue_segments(db)


@router.get(
    "/stocks/{symbol}/revenue-segments",
    response_model=RevenueSegmentationResponse,
)
def get_revenue_segments_endpoint(
    symbol: str,
    response: Response,
    use_case: GetRevenueSegments = Depends(get_get_revenue_segments),
) -> RevenueSegmentationResponse:
    try:
        segmentation = use_case.run(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Domain errors (StockNotFound → 404, StockDataUnavailable → 502) are translated by
    # the central handlers in endpoints/error_handlers.py.
    # Segment data moves once a year (on a filing) and is served from the DB cache, so
    # cache briefly: a burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return RevenueSegmentationResponse.from_segmentation(segmentation)
