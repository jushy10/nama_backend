"""HTTP API for reading the screened stock universe — the universe slice's read side.

Two read endpoints over the ``stocks`` anchor the universe sync populates (the read side the
slice deferred until now; its write side is the ``/internal/universe/sync`` cron):

- ``GET /stocks/ticker`` — a paginated search/filter/sort over the screened universe: a
  free-text ``q`` matched (case-insensitive substring) against name *or* ticker (so ``NV``
  surfaces Nvidia and NVDA), ``sector`` / ``industry`` slug filters, the ``in_sp500`` /
  ``in_nasdaq100`` membership flags, and a ``sort`` (market cap default, revenue or EPS growth)
  with an ``order``. Rows are DB facts only — no live price; the FE fetches a quote or the full
  card per row via ``GET /stocks/ticker/{ticker}`` (the single-symbol sibling of this list).
- ``GET /stocks/classifications`` — the distinct sector + industry slugs, for the FE's filter
  menus.

Controller + presenter + wiring, the composition-root way, in ``app/stocks/endpoints/`` like
the other slices' HTTP. Neither endpoint touches a vendor or needs an API key — both read only
the database, so the use cases are always constructable and the only error a request maps to is
a 400 (a malformed ``sort``/``order`` is caught earlier by FastAPI's enum binding as a 422).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.universe.db_repository import SqlStockSearchRepository
from app.stocks.universe.entities import (
    Classifications,
    SortDirection,
    StockSearchPage,
    StockSort,
)
from app.stocks.universe.schemas import (
    ClassificationsResponse,
    StockSearchItemResponse,
    StockSearchResponse,
)
from app.stocks.universe.use_cases import ListClassifications, SearchStocks

router = APIRouter(tags=["universe"])


def get_search_use_case(db: Session = Depends(get_db)) -> SearchStocks:
    # Pure DB read over the shared anchor — no vendor, no key to gate on. The repository is
    # request-scoped, like the session.
    return SearchStocks(SqlStockSearchRepository(db))


def get_classifications_use_case(db: Session = Depends(get_db)) -> ListClassifications:
    return ListClassifications(SqlStockSearchRepository(db))


def _present_search(page: StockSearchPage) -> StockSearchResponse:
    """Presenter: search-page entity -> HTTP response DTO."""
    return StockSearchResponse(
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        count=len(page.results),
        results=[
            StockSearchItemResponse(
                ticker=r.ticker,
                name=r.name,
                sector=r.sector,
                industry=r.industry,
                market_cap=r.market_cap,
                revenue_growth_yoy=r.revenue_growth_yoy,
                eps_growth_yoy=r.eps_growth_yoy,
                in_sp500=r.in_sp500,
                in_nasdaq100=r.in_nasdaq100,
            )
            for r in page.results
        ],
    )


def _present_classifications(c: Classifications) -> ClassificationsResponse:
    """Presenter: classifications entity -> HTTP response DTO."""
    return ClassificationsResponse(
        sectors=list(c.sectors), industries=list(c.industries)
    )


@router.get("/stocks/ticker", response_model=StockSearchResponse)
def search_stocks_endpoint(
    response: Response,
    q: str | None = Query(
        None,
        description=(
            "Free-text search, matched as a case-insensitive substring against the company "
            "name OR the ticker (so 'NV' returns Nvidia and NVDA). Omit to browse the universe."
        ),
    ),
    sector: str | None = Query(
        None,
        description=(
            "Filter to one sector. Accepts the slug from /stocks/classifications "
            "(e.g. 'technology') or the raw label ('Technology')."
        ),
    ),
    industry: str | None = Query(
        None,
        description=(
            "Filter to one industry. Accepts the slug from /stocks/classifications "
            "(e.g. 'semiconductors') or the raw label."
        ),
    ),
    in_sp500: bool | None = Query(
        None, description="Filter by S&P 500 membership. Omit for both members and non-members."
    ),
    in_nasdaq100: bool | None = Query(
        None, description="Filter by Nasdaq-100 membership. Omit for both."
    ),
    sort: StockSort = Query(
        StockSort.MARKET_CAP,
        description="Sort field: market_cap (default), revenue_growth, or eps_growth.",
    ),
    order: SortDirection = Query(
        SortDirection.DESC, description="Sort direction: asc or desc (default)."
    ),
    limit: int = Query(
        SearchStocks.DEFAULT_LIMIT,
        ge=1,
        le=SearchStocks.MAX_LIMIT,
        description="Page size (max 100).",
    ),
    offset: int = Query(0, ge=0, description="Rows to skip, for pagination."),
    use_case: SearchStocks = Depends(get_search_use_case),
) -> StockSearchResponse:
    try:
        page = use_case.execute(
            query=q,
            sector=sector,
            industry=industry,
            in_sp500=in_sp500,
            in_nasdaq100=in_nasdaq100,
            sort=sort,
            direction=order,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # The universe is slow-moving (refreshed out of band by the sync cron) and this is a plain
    # DB read — cache briefly so a burst of viewers (and any CDN in front) collapses onto one
    # query without going stale.
    response.headers["Cache-Control"] = "public, max-age=60"
    return _present_search(page)


@router.get("/stocks/classifications", response_model=ClassificationsResponse)
def list_classifications_endpoint(
    response: Response,
    use_case: ListClassifications = Depends(get_classifications_use_case),
) -> ClassificationsResponse:
    classifications = use_case.execute()
    # These barely change (a new sector/industry only surfaces as the universe grows), so cache
    # longer than the search list.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present_classifications(classifications)
