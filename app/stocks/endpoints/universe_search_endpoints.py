"""HTTP API for searching the stock universe.

``GET /stocks/search?q=…`` — the read endpoint for the universe slice: find companies in the
screened US ≥$1B universe by ticker or company name, largest market cap first. This is the
app's only *discovery* route; every other stocks endpoint is keyed on a symbol you already
know. It reads the screened ``stocks`` anchor rows populated out of band by the universe
cron (those carrying a ``market_cap``), so it never touches a vendor.

Controller + presenter + wiring, the composition-root way, sitting in
``app/stocks/endpoints/`` beside the cron entrypoint (``cron_universe_endpoints``) so all of
the slice's HTTP lives in one place. The static ``/stocks/search`` path doesn't collide with
the ``/stocks/{symbol}/…`` reads (different structure, no bare ``/stocks/{symbol}``).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.universe.db_repository import SqlUniverseRepository
from app.stocks.universe.entities import ScreenedStock
from app.stocks.universe.schemas import StockSearchResponse, StockSearchResult
from app.stocks.universe.use_cases import SearchStocks

router = APIRouter(tags=["stocks"])


def get_search_stocks(db: Session = Depends(get_db)) -> SearchStocks:
    # DB-only read over the universe the cron fills; no vendor, no key to gate on.
    return SearchStocks(SqlUniverseRepository(db))


def _present(result: ScreenedStock) -> StockSearchResult:
    """Presenter: screened-stock entity -> HTTP response DTO."""
    return StockSearchResult(
        ticker=result.ticker,
        name=result.name,
        exchange=result.exchange,
        market_cap=result.market_cap,
        sector=result.sector,
    )


@router.get("/stocks/search", response_model=StockSearchResponse)
def search_stocks_endpoint(
    response: Response,
    q: str = Query(
        ...,
        min_length=1,
        description="Ticker or company-name substring to match (case-insensitive).",
    ),
    limit: int = Query(
        SearchStocks.DEFAULT_LIMIT,
        ge=1,
        le=SearchStocks.MAX_LIMIT,
        description="Max results to return, largest market cap first.",
    ),
    use_case: SearchStocks = Depends(get_search_stocks),
) -> StockSearchResponse:
    try:
        results = use_case.execute(q, limit=limit)
    except ValueError as exc:
        # e.g. a whitespace-only query that normalizes to empty.
        raise HTTPException(400, str(exc)) from exc
    # The universe changes only when the weekly cron runs, so cache briefly: a burst of
    # keystroke-driven searches collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=60"
    return StockSearchResponse(
        query=q.strip(),
        count=len(results),
        results=[_present(result) for result in results],
    )
