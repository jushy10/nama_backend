"""HTTP API for the ETF collection — the top-ETFs search + the category filter menu.

- ``GET /stocks/etfs`` — a paginated search/filter/sort over the screened top-ETF set stored in
  the ``etfs`` table: a free-text ``q`` matched case-insensitively against name *or* ticker, a
  ``category`` slug filter (the fund type), and a ``sort`` (net assets — the "top" default — or
  expense ratio) with an ``order``. Rows are stored facts only — no live price; a client opens
  the shared ``GET /stocks/{symbol}/quote`` for a live ETF quote (Alpaca serves ETFs too).
- ``GET /stocks/etfs/categories`` — the distinct category slugs, for the FE's filter menu.

Pure DB read (``SqlEtfSearchRepository`` → ``SearchEtfs`` / ``ListEtfCategories``), no vendor or
key, so the only request error is a 400 (a bad ``sort``/``order`` is a 422 from the enum
binding). The refresh that populates the table (screen + category enrichment) is the separate
cron endpoint (``POST /internal/etfs/sync``).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.etfs.db_repository import SqlEtfSearchRepository
from app.stocks.etfs.entities import (
    EtfCategories,
    EtfSearchPage,
    EtfSort,
    SortDirection,
)
from app.stocks.etfs.schemas import (
    EtfCategoriesResponse,
    EtfSearchItemResponse,
    EtfSearchResponse,
)
from app.stocks.etfs.use_cases import ListEtfCategories, SearchEtfs

router = APIRouter(tags=["etfs"])


def get_search_use_case(db: Session = Depends(get_db)) -> SearchEtfs:
    # Pure DB read over the etfs table — no vendor, no key to gate on. The repository is
    # request-scoped, like the session.
    return SearchEtfs(SqlEtfSearchRepository(db))


def get_categories_use_case(db: Session = Depends(get_db)) -> ListEtfCategories:
    return ListEtfCategories(SqlEtfSearchRepository(db))


def _present_search(page: EtfSearchPage) -> EtfSearchResponse:
    """Presenter: search-page entity -> HTTP response DTO."""
    return EtfSearchResponse(
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        count=len(page.results),
        results=[
            EtfSearchItemResponse(
                ticker=r.ticker,
                name=r.name,
                exchange=r.exchange,
                net_assets=r.net_assets,
                expense_ratio=r.expense_ratio,
                category=r.category,
            )
            for r in page.results
        ],
    )


def _present_categories(categories: EtfCategories) -> EtfCategoriesResponse:
    """Presenter: categories entity -> HTTP response DTO."""
    return EtfCategoriesResponse(categories=list(categories.categories))


@router.get("/stocks/etfs", response_model=EtfSearchResponse)
def search_etfs_endpoint(
    response: Response,
    q: str | None = Query(
        None,
        description=(
            "Free-text search, matched as a case-insensitive substring against the fund name OR "
            "the ticker (so 'gold' returns gold-miner ETFs and 'SPY' matches by ticker). Omit to "
            "browse the top ETFs."
        ),
    ),
    category: str | None = Query(
        None,
        description=(
            "Filter to one fund category (the ETF type). Accepts the slug from "
            "/stocks/etfs/categories (e.g. 'large_growth') or the raw label ('Large Growth')."
        ),
    ),
    sort: EtfSort = Query(
        EtfSort.NET_ASSETS,
        description=(
            "Sort field: net_assets (assets under management, default — the biggest/top funds) "
            "or expense_ratio (pair with order=asc for cheapest first)."
        ),
    ),
    order: SortDirection = Query(
        SortDirection.DESC, description="Sort direction: asc or desc (default)."
    ),
    limit: int = Query(
        SearchEtfs.DEFAULT_LIMIT,
        ge=1,
        le=SearchEtfs.MAX_LIMIT,
        description="Page size (max 100).",
    ),
    offset: int = Query(0, ge=0, description="Rows to skip, for pagination."),
    use_case: SearchEtfs = Depends(get_search_use_case),
) -> EtfSearchResponse:
    try:
        page = use_case.execute(
            query=q, category=category, sort=sort, direction=order, limit=limit, offset=offset
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # The set is slow-moving (refreshed out of band by the sync cron) and this is a plain DB
    # read — cache briefly so a burst of viewers (and any CDN in front) collapses onto one query
    # without going stale.
    response.headers["Cache-Control"] = "public, max-age=60"
    return _present_search(page)


@router.get("/stocks/etfs/categories", response_model=EtfCategoriesResponse)
def list_etf_categories_endpoint(
    response: Response,
    use_case: ListEtfCategories = Depends(get_categories_use_case),
) -> EtfCategoriesResponse:
    categories = use_case.execute()
    # These barely change (a new category only surfaces as the set grows), so cache longer than
    # the search list.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present_categories(categories)
