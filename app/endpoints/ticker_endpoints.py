import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.domains.financials.earnings.quarterly.interfaces import QuarterlyEarningsAdapter
from app.endpoints.quarterly_earnings_endpoints import (
    get_quarterly_earnings_provider,
)
from app.domains.shared.exceptions import StockDataUnavailable
from app.domains.shared.interfaces import AnalystEstimatesAdapter
from app.adapters.bedrock.screener_query_adapter_impl import (
    ScreenerQueryAdapterImpl,
)
from app.endpoints.wiring import (
    get_estimates_provider,
    get_options_provider,
    get_price_provider,
)
from app.domains.pricing.ticker import wiring as ticker_wiring
from app.domains.pricing.ticker.api_schemas import (
    PeHistoryResponse,
    TickerCardResponse,
    TickerTypeResponse,
)
from app.domains.pricing.ticker.interfaces import OptionChainAdapter
from app.domains.pricing.ticker.use_cases import (
    ClassifyTicker,
    GetStockPeHistory,
    GetTickerCard,
)
from app.domains.listings.universe.repository_adapter_impl import StockSearchRepositoryAdapterImpl
from app.domains.listings.universe.entities import (
    Classifications,
    IndustryValuation,
    MarketCapTier,
    PeerCompany,
    PeerComparison,
    ScreenIntent,
    SortDirection,
    StockSearchPage,
    StockSort,
)
from app.domains.listings.universe.interfaces import ScreenerQueryAdapter
from app.domains.listings.universe.schemas import (
    AiScreenInterpretationResponse,
    AiScreenResponse,
    ClassificationsResponse,
    IndustryValuationResponse,
    PeerCompanyResponse,
    PeerComparisonResponse,
    PeerMediansResponse,
    StockSearchItemResponse,
    StockSearchResponse,
)
from app.domains.listings.universe.use_cases import (
    AiScreenStocks,
    GetIndustryValuation,
    GetPeerComparison,
    ListClassifications,
    SearchStocks,
)

router = APIRouter(tags=["ticker"])


def get_ticker_card_use_case(
    provider=Depends(get_price_provider),
    options: OptionChainAdapter = Depends(get_options_provider),
    earnings: QuarterlyEarningsAdapter = Depends(get_quarterly_earnings_provider),
    estimates: AnalystEstimatesAdapter = Depends(get_estimates_provider),
    db: Session = Depends(get_db),
) -> GetTickerCard:
    # Depends shim over the slice's wiring — exists for the db lifecycle, the shared
    # provider singletons (the market-routing price router with its Alpaca 503 gate, the
    # keyless options chain, the quarterly DB cache, the DB-only estimates projection),
    # and the dependency_overrides test seam.
    return ticker_wiring.build_get_ticker_card(
        db, provider, options, earnings, estimates
    )


@router.get("/stocks/ticker/{ticker}", response_model=TickerCardResponse)
def get_ticker_card_endpoint(
    ticker: str,
    response: Response,
    include: list[str] | None = Query(
        default=None,
        description=(
            "Opt-in blocks to include: dividend, performance, metrics, "
            "options_metrics. Repeat the param or comma-separate "
            "(?include=dividend,metrics). Unrequested blocks are null and cost "
            "no upstream call."
        ),
    ),
    use_case: GetTickerCard = Depends(get_ticker_card_use_case),
) -> TickerCardResponse:
    try:
        # Bad request input (malformed symbol / unknown include) surfaces as a ValueError —
        # an inline 400, deliberately kept in the endpoint. Domain errors (StockNotFound →
        # 404, StockDataUnavailable → 502) are translated by the central handlers.
        card = use_case.run(ticker, include=include)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # A valuation card, not a ticking price: the consensus legs move on analyst
    # revisions and the multiple doesn't need tick precision, so cache briefly —
    # a burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return TickerCardResponse.from_card(card)


def get_pe_history_use_case(
    provider=Depends(get_price_provider),
) -> GetStockPeHistory:
    # Depends shim over the slice's wiring: the market-routing provider supplies the daily
    # closes (it implements CandleAdapter — the same instance the candle chart uses,
    # US→Alpaca / CA→Yahoo, inheriting the Alpaca 503 gate for a US symbol); the deep
    # reported-EPS leg is the slice's keyless yfinance singleton.
    return ticker_wiring.build_get_stock_pe_history(provider)


@router.get(
    "/stocks/ticker/{ticker}/pe-history", response_model=PeHistoryResponse
)
def get_pe_history_endpoint(
    ticker: str,
    response: Response,
    use_case: GetStockPeHistory = Depends(get_pe_history_use_case),
) -> PeHistoryResponse:
    try:
        history = use_case.run(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # A historical series that only extends when a new quarter reports (its latest point
    # tailed by today's close) — cache an hour, like the other slow-moving card reads.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return PeHistoryResponse.from_history(history)


def _round2(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def get_peer_comparison_use_case(
    db: Session = Depends(get_db),
) -> GetPeerComparison:
    # Same request-scoped read repository the search / industry-P/E reads use — a pure DB read
    # over the shared anchor, no vendor or key.
    return GetPeerComparison(StockSearchRepositoryAdapterImpl(db))


def _present_peer_company(company: PeerCompany) -> PeerCompanyResponse:
    return PeerCompanyResponse(
        ticker=company.ticker,
        name=company.name,
        market_cap=company.market_cap,
        pe_ratio=company.pe_ratio,
        ev_ebitda=company.ev_ebitda,
        fcf_yield=company.fcf_yield,
        net_margin=_round2(company.net_margin),
        revenue_growth_yoy=company.revenue_growth_yoy,
        is_anchor=company.is_anchor,
    )


def _present_peer_comparison(comparison: PeerComparison) -> PeerComparisonResponse:
    medians = comparison.medians
    return PeerComparisonResponse(
        ticker=comparison.ticker,
        industry=comparison.industry,
        cohort=comparison.cohort,
        count=len(comparison.peers),
        anchor=(
            _present_peer_company(comparison.anchor)
            if comparison.anchor is not None
            else None
        ),
        peers=[_present_peer_company(p) for p in comparison.peers],
        medians=PeerMediansResponse(
            pe_ratio=medians.pe_ratio,
            ev_ebitda=medians.ev_ebitda,
            fcf_yield=medians.fcf_yield,
            net_margin=_round2(medians.net_margin),
            revenue_growth_yoy=medians.revenue_growth_yoy,
        ),
    )


@router.get(
    "/stocks/ticker/{ticker}/peers", response_model=PeerComparisonResponse
)
def get_peer_comparison_endpoint(
    ticker: str,
    response: Response,
    use_case: GetPeerComparison = Depends(get_peer_comparison_use_case),
) -> PeerComparisonResponse:
    try:
        comparison = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # A plain DB read over the slow-moving universe (refreshed out of band by the syncs) — cache
    # briefly like the search list so a burst of viewers collapses onto one query.
    response.headers["Cache-Control"] = "public, max-age=60"
    return _present_peer_comparison(comparison)


def get_classify_ticker_use_case(db: Session = Depends(get_db)) -> ClassifyTicker:
    # Depends shim over the slice's wiring — a pure DB read (a single indexed membership
    # check against the etfs table), always constructable.
    return ticker_wiring.build_classify_ticker(db)


@router.get("/stocks/type/{ticker}", response_model=TickerTypeResponse)
def get_ticker_type_endpoint(
    ticker: str,
    response: Response,
    use_case: ClassifyTicker = Depends(get_classify_ticker_use_case),
) -> TickerTypeResponse:
    try:
        result = use_case.run(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # ETF-universe membership only shifts when the screen re-syncs (rare), so cache
    # generously — a burst of classifier calls collapses onto one read.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return TickerTypeResponse.from_classification(result)


# --- The universe search + filter menus (the /stocks/ticker collection) -------------------
#
# The read side of the universe slice, grouped here beside the card because they share the
# /stocks/ticker resource. Both read only the shared `stocks` anchor — no vendor, no key — so
# the use cases are always constructable.


def get_search_use_case(db: Session = Depends(get_db)) -> SearchStocks:
    # Pure DB read over the shared anchor — no vendor, no key to gate on. The repository is
    # request-scoped, like the session.
    return SearchStocks(StockSearchRepositoryAdapterImpl(db))


def get_classifications_use_case(db: Session = Depends(get_db)) -> ListClassifications:
    return ListClassifications(StockSearchRepositoryAdapterImpl(db))


def get_industry_valuation_use_case(
    db: Session = Depends(get_db),
) -> GetIndustryValuation:
    # Same request-scoped read repository the search uses — a pure DB read over the anchor.
    return GetIndustryValuation(StockSearchRepositoryAdapterImpl(db))


def _present_search(page: StockSearchPage) -> StockSearchResponse:
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
                pe_ratio=r.pe_ratio,
                fcf_yield=r.fcf_yield,
                ev_ebitda=r.ev_ebitda,
                revenue_growth_yoy=r.revenue_growth_yoy,
                eps_growth_yoy=r.eps_growth_yoy,
                fcf_growth_yoy=r.fcf_growth_yoy,
                forward_revenue_growth_yoy=r.forward_revenue_growth_yoy,
                forward_eps_growth_yoy=r.forward_eps_growth_yoy,
                in_sp500=r.in_sp500,
                in_nasdaq100=r.in_nasdaq100,
                country=r.country,
                currency=r.currency,
                has_us_listing=r.has_us_listing,
            )
            for r in page.results
        ],
    )


def _present_classifications(c: Classifications) -> ClassificationsResponse:
    return ClassificationsResponse(
        sectors=list(c.sectors), industries=list(c.industries)
    )


def _present_industry_valuation(
    v: IndustryValuation,
) -> IndustryValuationResponse:
    return IndustryValuationResponse(
        industry=v.industry,
        count=v.count,
        median_pe=v.median_pe,
        p25_pe=v.p25_pe,
        p75_pe=v.p75_pe,
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
    sector: list[str] | None = Query(
        None,
        description=(
            "Filter by sector. Repeat to match several at once "
            "(?sector=technology&sector=energy — an OR set). Each accepts the slug from "
            "/stocks/classifications (e.g. 'technology') or the raw label ('Technology'). "
            "Omit for every sector."
        ),
    ),
    industry: list[str] | None = Query(
        None,
        description=(
            "Filter by industry. Repeat to match several at once (an OR set). Each accepts the "
            "slug from /stocks/classifications (e.g. 'semiconductors') or the raw label. "
            "Omit for every industry."
        ),
    ),
    in_sp500: bool | None = Query(
        None, description="Filter by S&P 500 membership. Omit for both members and non-members."
    ),
    in_nasdaq100: bool | None = Query(
        None, description="Filter by Nasdaq-100 membership. Omit for both."
    ),
    country: list[str] | None = Query(
        None,
        description=(
            "Filter by listing market as an ISO-2 code: us or ca. Repeat to match the union "
            "(?country=us&country=ca). Omit for every market. Filtering to one market keeps a "
            "market_cap sort within a single currency (the ≥$1B floor is applied natively per "
            "market, so a CAD cap isn't comparable to a USD one)."
        ),
    ),
    include_interlisted: bool = Query(
        False,
        description=(
            "When a single market is chosen, scope it to that market's home companies (the "
            "default): the US market drops Canadian companies' US listings (e.g. CNI) by issuer "
            "domicile while keeping other foreign ADRs, and the Canadian market drops the CDRs of "
            "US / foreign companies -- structurally by excluding the Cboe Canada (.NE) "
            "depositary-receipt venue, plus any confirmed foreign-domiciled listing -- while "
            "keeping Canadian companies (TSX / TSXV). Set true to skip that scoping and see every "
            "listing in the market, cross-listed duplicates included. No effect when zero or "
            "several markets are chosen."
        ),
    ),
    market_cap: list[MarketCapTier] | None = Query(
        None,
        description=(
            "Filter by market-cap tier: mega (>= $200B), large ($10-200B), mid ($2-10B), "
            "or small ($250M-$2B). Repeat to match the union of several tiers "
            "(?market_cap=large&market_cap=mid). Omit for every size."
        ),
    ),
    sort: StockSort | None = Query(
        None,
        description=(
            "Sort field. Omit for no sort (a neutral A->Z by ticker). Otherwise: market_cap; "
            "the trailing growth figures revenue_growth, eps_growth, or growth (their "
            "equal-weight blend); their forward (FY1->FY2 consensus) counterparts "
            "forward_revenue_growth, forward_eps_growth, forward_growth; pe (trailing P/E on "
            "the consensus basis; ascending surfaces the cheapest on earnings); or ev_ebitda "
            "(EV/EBITDA; ascending surfaces the cheapest on enterprise value). Stocks missing "
            "the chosen figure sort last."
        ),
    ),
    order: SortDirection = Query(
        SortDirection.DESC,
        description="Sort direction: asc or desc (default). Ignored when no sort is given.",
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
            sectors=sector,
            industries=industry,
            in_sp500=in_sp500,
            in_nasdaq100=in_nasdaq100,
            market_cap_tiers=market_cap,
            sort=sort,
            direction=order,
            limit=limit,
            offset=offset,
            countries=country,
            include_interlisted=include_interlisted,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # The universe is slow-moving (refreshed out of band by the sync cron) and this is a plain
    # DB read — cache briefly so a burst of viewers (and any CDN in front) collapses onto one
    # query without going stale.
    response.headers["Cache-Control"] = "public, max-age=60"
    return _present_search(page)


@lru_cache(maxsize=1)
def get_screener_translator() -> ScreenerQueryAdapter:
    # The AI screener's translation is its primary data (its reason to exist), so it's
    # required — but like the analysis providers there's no secret to gate on: Bedrock
    # authenticates through the process's AWS credentials (the ECS task role in
    # production). Region + model id are config with sane defaults (the id may be a
    # cross-region inference profile); BEDROCK_SCREENER_MODEL_ID overrides the model
    # independently of the analysis providers. A missing 'bedrock' extra surfaces as a
    # clean 503 here rather than a 500.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_SCREENER_MODEL_ID")
    try:
        if model_id:
            return ScreenerQueryAdapterImpl(model_id=model_id, region=region)
        return ScreenerQueryAdapterImpl(region=region)
    except ImportError as exc:
        raise HTTPException(
            503, "AI stock screening is not configured (install the 'bedrock' extra)."
        ) from exc


def get_ai_search_use_case(
    db: Session = Depends(get_db),
    translator: ScreenerQueryAdapter = Depends(get_screener_translator),
) -> AiScreenStocks:
    # The AI screen only translates — it reads the universe's classifications (the
    # translator's allowed vocabulary) but does not run the search itself. The translator
    # (Bedrock) is the only non-DB dependency — it carries its own 503 gate in the wiring.
    return AiScreenStocks(translator, StockSearchRepositoryAdapterImpl(db))


def _present_ai_screen(intent: ScreenIntent) -> AiScreenResponse:
    return AiScreenResponse(
        interpreted=AiScreenInterpretationResponse(
            query=intent.query,
            sectors=list(intent.sectors),
            industries=list(intent.industries),
            in_sp500=intent.in_sp500,
            in_nasdaq100=intent.in_nasdaq100,
            market_cap_tiers=[t.value for t in intent.market_cap_tiers],
            sort=intent.sort.value if intent.sort is not None else None,
            direction=intent.direction.value,
            limit=intent.limit,
        ),
    )


@router.get("/stocks/ai-search", response_model=AiScreenResponse)
def ai_search_stocks_endpoint(
    response: Response,
    q: str = Query(
        ...,
        min_length=1,
        description=(
            "A plain-English screen request — e.g. 'mega-cap technology stocks', "
            "'semiconductor companies', or 'top S&P 500 names by revenue growth'. An AI "
            "translates it into the same filters the manual /stocks/ticker search accepts and "
            "returns just those interpreted filters (it does not run the search) — the client "
            "applies them to /stocks/ticker to fetch the rows, so it can show and edit them."
        ),
    ),
    use_case: AiScreenStocks = Depends(get_ai_search_use_case),
) -> AiScreenResponse:
    try:
        intent = use_case.execute(query=q)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(
            502, "AI stock screening is temporarily unavailable."
        ) from exc
    # Deterministic for a given request against the slow-moving universe — cache briefly like
    # the manual search so a burst of identical queries collapses onto one translation.
    response.headers["Cache-Control"] = "public, max-age=60"
    return _present_ai_screen(intent)


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


@router.get(
    "/stocks/industries/{industry}/pe", response_model=IndustryValuationResponse
)
def industry_pe_endpoint(
    industry: str,
    response: Response,
    use_case: GetIndustryValuation = Depends(get_industry_valuation_use_case),
) -> IndustryValuationResponse:
    try:
        valuation = use_case.execute(industry)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # A plain DB read over the slow-moving universe (refreshed out of band by the sync) — cache
    # briefly like the search list so a burst of viewers collapses onto one query.
    response.headers["Cache-Control"] = "public, max-age=60"
    return _present_industry_valuation(valuation)
