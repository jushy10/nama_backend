from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.domains.coverage.recommendations import wiring
from app.domains.coverage.recommendations.api_schemas import AnalystInfoResponse
from app.domains.coverage.recommendations.use_cases import GetStockAnalystInfo

router = APIRouter(tags=["analyst-info"])


def get_get_stock_analyst_info(db: Session = Depends(get_db)) -> GetStockAnalystInfo:
    # Depends shim over the slice's wiring — exists for the db lifecycle and the
    # dependency_overrides test seam, nothing more.
    return wiring.build_get_stock_analyst_info(db)


@router.get(
    "/stocks/ticker/{ticker}/analyst-info", response_model=AnalystInfoResponse
)
def get_stock_analyst_info_endpoint(
    ticker: str,
    response: Response,
    use_case: GetStockAnalystInfo = Depends(get_get_stock_analyst_info),
) -> AnalystInfoResponse:
    try:
        info = use_case.run(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # The trends are primary, so their StockNotFound/StockDataUnavailable propagate to the
    # central handlers (404/502); the rating-change leg is best-effort inside the use case.
    # Analyst coverage moves slowly (monthly snapshots + accreting events, served from the DB
    # cache), so cache briefly: a burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return AnalystInfoResponse.from_info(info)
