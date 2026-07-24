from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.domains.coverage.news import wiring
from app.domains.coverage.news.api_schemas import StockNewsResponse
from app.domains.coverage.news.use_cases import GetStockNews

router = APIRouter(tags=["news"])


def get_get_stock_news(db: Session = Depends(get_db)) -> GetStockNews:
    # Depends shim over the slice's wiring — exists for the db lifecycle and the
    # dependency_overrides test seam, nothing more.
    return wiring.build_get_stock_news(db)


@router.get("/stocks/{symbol}/news", response_model=StockNewsResponse)
def get_stock_news_endpoint(
    symbol: str,
    response: Response,
    use_case: GetStockNews = Depends(get_get_stock_news),
) -> StockNewsResponse:
    try:
        news = use_case.run(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Domain errors (StockNotFound → 404, StockDataUnavailable → 502) are translated by
    # the central handlers in endpoints/error_handlers.py.
    # News is served from the DB cache and refreshed out of band, so cache briefly: a
    # burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return StockNewsResponse.from_news(news)
