"""HTTP API for the ETF collection — the top-ETFs search, the category filter menu, and one
fund's detail card.

- ``GET /stocks/etfs`` — a paginated search/filter/sort over the screened top-ETF set stored in
  the ``etfs`` table: a free-text ``q`` matched case-insensitively against name *or* ticker, a
  ``category`` slug filter (the fund type), and a ``sort`` (net assets — the "top" default — or
  expense ratio) with an ``order``. Rows are stored facts only — no live price; a client opens
  the shared ``GET /stocks/{symbol}/quote`` for a live ETF quote (Alpaca serves ETFs too).
- ``GET /stocks/etfs/categories`` — the distinct category slugs, for the FE's filter menu.
- ``GET /stocks/etf/{ticker}`` — one fund's detail card: the **live quote** (Alpaca, primary —
  the same feed the quote endpoint uses, so a quote failure is the same 502), the stored
  ``etfs``-table facts (name/exchange/category), and the always-on best-effort Yahoo (``yfinance``)
  enrichment (fund family, description, top holdings, sector weightings). Then **opt-in blocks** via
  ``?include=`` (repeat or comma-separate; an unknown value is a 400): ``metrics`` (expense ratio,
  NAV, net assets), ``dividends`` (yield), and ``performance`` (the ``1w``/``1m``/``3m``/``6m``/
  ``ytd``/``1y`` trailing returns — the same gains format the stock endpoints serve — plus Yahoo's
  3y/5y annualized returns). A symbol that isn't in the stored ETF universe is a **404** ("not an
  ETF"). The Yahoo half never sinks the card — a blocked read just leaves those fields null/empty
  on a 200. Pay-per-use only bites on ``performance`` (its own Alpaca call, made just when asked
  for); ``metrics``/``dividends`` ride the always-fetched profile, so they cost no extra call.

The two list routes are pure DB reads (``SqlEtfSearchRepository`` → ``SearchEtfs`` /
``ListEtfCategories``), no vendor or key, so their only request error is a 400 (a bad
``sort``/``order`` is a 422 from the enum binding). The detail route reuses the composition root's
Alpaca provider (whose missing-keys 503 it inherits — the quote is primary) for both the quote and
the opt-in trailing-return windows (it implements both ports), plus the keyless yfinance ETF-profile
adapter (best-effort). The refresh that populates the table (screen + category enrichment) is the
separate cron endpoint (``POST /internal/etfs/sync``).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.yfinance_etf_profile_adapter import YfinanceEtfProfileProvider
from app.stocks.etfs.db_repository import (
    SqlEtfLookupRepository,
    SqlEtfSearchRepository,
)
from app.stocks.etfs.entities import (
    EtfCategories,
    EtfDetail,
    EtfSearchPage,
    EtfSort,
    SortDirection,
)
from app.stocks.etfs.ports import EtfProfileProvider
from app.stocks.etfs.schemas import (
    EtfCategoriesResponse,
    EtfDetailResponse,
    EtfDividendsResponse,
    EtfHoldingResponse,
    EtfMetricsResponse,
    EtfPerformanceResponse,
    EtfSearchItemResponse,
    EtfSearchResponse,
    EtfSectorWeightResponse,
)
from app.stocks.etfs.use_cases import GetEtfDetail, ListEtfCategories, SearchEtfs
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import StockPerformanceProvider, StockQuoteProvider
from app.stocks.router import get_provider

router = APIRouter(tags=["etfs"])


def get_search_use_case(db: Session = Depends(get_db)) -> SearchEtfs:
    # Pure DB read over the etfs table — no vendor, no key to gate on. The repository is
    # request-scoped, like the session.
    return SearchEtfs(SqlEtfSearchRepository(db))


def get_categories_use_case(db: Session = Depends(get_db)) -> ListEtfCategories:
    return ListEtfCategories(SqlEtfSearchRepository(db))


def get_etf_profile_provider() -> EtfProfileProvider:
    # The detail card's Yahoo enrichment — keyless yfinance, like the ETF category/screener
    # sources. Best-effort by contract (the provider never raises), so it's always wired.
    return YfinanceEtfProfileProvider()


def get_etf_detail_use_case(
    provider: StockQuoteProvider = Depends(get_provider),
    profile: EtfProfileProvider = Depends(get_etf_profile_provider),
    db: Session = Depends(get_db),
) -> GetEtfDetail:
    # The Alpaca singleton backs the live quote AND the opt-in trailing-return windows (the same
    # instance the quote/ticker endpoints use, so the fund's move never disagrees) — it implements
    # both StockQuoteProvider and StockPerformanceProvider, so the one dependency serves both roles
    # (the isinstance mirrors the ticker card's wiring). The lookup repository is the request-scoped
    # read over the etfs table (the membership gate + the stored facts), and the profile is the
    # keyless yfinance enrichment.
    performance = provider if isinstance(provider, StockPerformanceProvider) else None
    return GetEtfDetail(SqlEtfLookupRepository(db), provider, profile, performance)


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


def _present_metrics(detail: EtfDetail) -> EtfMetricsResponse:
    """The ``metrics`` block: the stored size/cost facts + Yahoo's NAV."""
    return EtfMetricsResponse(
        expense_ratio=detail.expense_ratio,
        nav=detail.profile.nav,
        net_assets=detail.net_assets,
    )


def _present_dividends(detail: EtfDetail) -> EtfDividendsResponse:
    """The ``dividends`` block: the fund's distribution yield (best-effort, off the profile)."""
    return EtfDividendsResponse(yield_percentage=detail.profile.dividend_yield)


def _present_performance(detail: EtfDetail) -> EtfPerformanceResponse:
    """The ``performance`` block: the Alpaca trailing windows (null when that best-effort read was
    blocked) plus Yahoo's 3y/5y annualized returns (off the always-fetched profile)."""
    perf = detail.performance
    p = detail.profile
    return EtfPerformanceResponse(
        one_week=perf.one_week if perf else None,
        one_month=perf.one_month if perf else None,
        three_month=perf.three_month if perf else None,
        six_month=perf.six_month if perf else None,
        ytd=perf.ytd if perf else None,
        one_year=perf.one_year if perf else None,
        three_year_return=p.three_year_return,
        five_year_return=p.five_year_return,
    )


def _present_detail(detail: EtfDetail) -> EtfDetailResponse:
    """Presenter: the assembled ETF detail -> HTTP response DTO.

    The live quote's move (change/change_percent) rides its entity's derived properties — the same
    rule as every other price view — while the stored facts and the profile's percent figures are
    passed through already normalized by the use case / adapter (no rounding here: the figures are
    the vendor's own, and rounding a percent like an expense ratio would lose precision). The
    always-on Yahoo enrichment (fund family, description, holdings, sector weightings) is served
    regardless; the metrics/dividends/performance blocks are emitted only when ``detail.include``
    says they were requested (``null`` otherwise)."""
    quote = detail.quote
    p = detail.profile
    return EtfDetailResponse(
        ticker=detail.ticker,
        name=detail.name,
        exchange=detail.exchange,
        price=quote.price,
        change=quote.change,
        change_percent=quote.change_percent,
        previous_close=quote.previous_close,
        as_of=quote.as_of,
        category=detail.category,
        fund_family=p.fund_family,
        description=p.description,
        top_holdings=[
            EtfHoldingResponse(ticker=h.ticker, name=h.name, weight=h.weight)
            for h in p.top_holdings
        ],
        sector_weightings=[
            EtfSectorWeightResponse(sector=s.sector, weight=s.weight)
            for s in p.sector_weightings
        ],
        metrics=_present_metrics(detail) if "metrics" in detail.include else None,
        dividends=_present_dividends(detail) if "dividends" in detail.include else None,
        performance=(
            _present_performance(detail) if "performance" in detail.include else None
        ),
    )


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


@router.get("/stocks/etf/{ticker}", response_model=EtfDetailResponse)
def get_etf_detail_endpoint(
    ticker: str,
    response: Response,
    include: list[str] | None = Query(
        default=None,
        description=(
            "Opt-in blocks to include: metrics (expense ratio, NAV, net assets), dividends "
            "(yield), performance (trailing returns). Repeat the param or comma-separate "
            "(?include=metrics,performance). Unrequested blocks are null; an unknown value is a 400."
        ),
    ),
    use_case: GetEtfDetail = Depends(get_etf_detail_use_case),
) -> EtfDetailResponse:
    try:
        detail = use_case.execute(ticker, include=include)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        # Not in the stored ETF universe (or a symbol with no data) -> "not an ETF".
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        # The primary source (the live quote) failed — same status the quote/ticker endpoints use.
        raise HTTPException(502, str(exc)) from exc
    # Built around the live quote, so it's not a static resource — but the stored facts and the
    # Yahoo profile move slowly, so cache briefly (like the ticker card) to collapse a burst of
    # viewers onto one upstream read without going stale.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present_detail(detail)
