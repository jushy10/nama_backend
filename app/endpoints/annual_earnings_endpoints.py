from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.domains.financials.earnings.annual import wiring
from app.domains.financials.earnings.annual.api_schemas import AnnualEarningsResponse
from app.domains.financials.earnings.annual.use_cases import GetAnnualEarnings

router = APIRouter(tags=["annual-earnings"])


def get_get_annual_earnings(db: Session = Depends(get_db)) -> GetAnnualEarnings:
    # Depends shim over the slice's wiring — exists for the db lifecycle and the
    # dependency_overrides test seam, nothing more.
    return wiring.build_get_annual_earnings(db)


@router.get("/stocks/{symbol}/earnings/annual", response_model=AnnualEarningsResponse)
def get_annual_earnings_endpoint(
    symbol: str,
    response: Response,
    use_case: GetAnnualEarnings = Depends(get_get_annual_earnings),
) -> AnnualEarningsResponse:
    try:
        timeline = use_case.run(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Domain errors (StockNotFound → 404, StockDataUnavailable → 502) are translated by
    # the central handlers in endpoints/error_handlers.py.
    # Reported years move slowly (and are served from the DB cache), so cache briefly: a
    # burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return AnnualEarningsResponse.from_timeline(timeline)
