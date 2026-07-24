from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.domains.financials.earnings.quarterly import wiring
from app.domains.financials.earnings.quarterly.api_schemas import QuarterlyEarningsResponse
from app.domains.financials.earnings.quarterly.interfaces import QuarterlyEarningsAdapter
from app.domains.financials.earnings.quarterly.use_cases import GetQuarterlyEarnings

router = APIRouter(tags=["quarterly-earnings"])


def get_quarterly_earnings_provider(
    db: Session = Depends(get_db),
) -> QuarterlyEarningsAdapter:
    # The db-cached provider shim — also injected into the ticker card (the trailing
    # P/E's TTM sum), so it lives beside the use-case shim rather than inside it.
    return wiring.build_quarterly_earnings_provider(db)


def get_get_quarterly_earnings(db: Session = Depends(get_db)) -> GetQuarterlyEarnings:
    # Depends shim over the slice's wiring — exists for the db lifecycle and the
    # dependency_overrides test seam, nothing more.
    return wiring.build_get_quarterly_earnings(db)


@router.get(
    "/stocks/{symbol}/earnings/quarterly", response_model=QuarterlyEarningsResponse
)
def get_quarterly_earnings_endpoint(
    symbol: str,
    response: Response,
    use_case: GetQuarterlyEarnings = Depends(get_get_quarterly_earnings),
) -> QuarterlyEarningsResponse:
    try:
        timeline = use_case.run(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Domain errors (StockNotFound → 404, StockDataUnavailable → 502) are translated by
    # the central handlers in endpoints/error_handlers.py.
    # Reported quarters and firmed-up report dates move slowly (and are served from the
    # DB cache), so cache briefly: a burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return QuarterlyEarningsResponse.from_timeline(timeline)
