"""HTTP API for the ETF collection — the top-ETFs search, the category filter menu, and one
fund's detail card.

- ``GET /stocks/etfs`` — a paginated search/filter/sort over the screened top-ETF set stored in
  the ``etfs`` table: a free-text ``q`` matched case-insensitively against name *or* ticker, a
  ``category`` slug filter (the fund type), and a ``sort`` (net assets — the "top" default — or
  expense ratio) with an ``order``. Rows are stored facts only — no live price; a client opens
  ``GET /stocks/etf/{ticker}`` (below) for a live ETF quote (Alpaca serves ETFs too).
- ``GET /stocks/etfs/categories`` — the distinct category slugs, for the FE's filter menu.
- ``GET /stocks/etf/{ticker}`` — one fund's detail card: the **live quote** (Alpaca, primary —
  the same Alpaca feed every price view uses, so a quote failure is a 502), the stored
  ``etfs``-table facts (name/exchange/category), and the stored profile (fund family, description,
  top holdings, sector weightings) read straight from the DB — populated out-of-band by the sync,
  so the base card makes no live Yahoo call. Then **opt-in blocks** via ``?include=`` (repeat
  or comma-separate; an unknown value is a 400): ``metrics`` (expense ratio, NAV, net assets),
  ``dividends`` (yield), and ``performance`` (the ``1w``/``1m``/``3m``/``6m``/``ytd``/``1y``
  trailing returns — the same gains format the stock endpoints serve — plus the 3y/5y annualized
  returns, which are no longer stored and so come from a live Yahoo read made only for this block).
  A symbol that isn't in the stored ETF universe is a **404** ("not an ETF"). A fund the sync
  hasn't enriched yet just serves null/empty profile fields on a 200. Pay-per-use bites on
  ``performance`` alone — it makes two live calls (the Alpaca windows + the Yahoo return ladder),
  both best-effort; ``metrics``/``dividends`` ride the DB-read profile + stored facts, no extra call.
- ``GET /stocks/etf/{ticker}/analysis`` — a plain-language, AI-generated buy/hold/sell read on the
  fund (the ETF sibling of ``GET /stocks/{symbol}/analysis``). Assembles the fund's snapshot (the
  same quote + stored facts + profile the detail card shows, with the trailing/long-term returns)
  and asks Claude on Bedrock for a balanced read grounded only in those figures. Same error map as
  the detail card (400 bad ticker / 404 not-an-ETF / 502 failed quote or model call), plus a 503
  from the wiring when the optional ``bedrock`` extra isn't installed. Cached 5 min — model calls
  are slow and metered.

The two list routes are pure DB reads (``SqlEtfSearchRepository`` → ``SearchEtfs`` /
``ListEtfCategories``), no vendor or key, so their only request error is a 400 (a bad
``sort``/``order`` is a 422 from the enum binding). The detail route reuses the composition root's
Alpaca provider (whose missing-keys 503 it inherits — the quote is primary) for both the quote and
the opt-in trailing-return windows (it implements both ports), a DB read of the stored profile, and
the keyless Yahoo profile provider that backs the performance block's live 3y/5y returns. The
refresh that populates the table + profile (screen + profile enrichment) is the separate cron
endpoint (``POST /internal/etfs/sync``).
"""

import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.bedrock.etf_analysis_adapter import BedrockEtfAnalysisProvider
from app.stocks.adapters.yfinance_etf_profile_adapter import (
    YfinanceEtfProfileProvider,
)
from app.stocks.analysis.db_repository import SqlInvestmentAnalysisCache
from app.stocks.analysis.entities import InvestmentAnalysis
from app.stocks.etfs.db_repository import (
    SqlEtfLookupRepository,
    SqlEtfSearchRepository,
)
from app.stocks.etfs.entities import (
    EtfCategories,
    EtfDetail,
    EtfScreenIntent,
    EtfSearchPage,
    EtfSort,
    SortDirection,
)
from app.stocks.etfs.ports import EtfAnalysisProvider, EtfScreenerQueryTranslator
from app.stocks.etfs.schemas import (
    AiEtfScreenInterpretationResponse,
    AiEtfScreenResponse,
    EtfAnalysisResponse,
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
from app.stocks.etfs.use_cases import (
    AiScreenEtfs,
    GetEtfAnalysis,
    GetEtfDetail,
    ListEtfCategories,
    SearchEtfs,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.analysis.ports import InvestmentAnalysisCache
from app.stocks.ports import (
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.adapters.bedrock.etf_screener_query_adapter import (
    BedrockEtfScreenerQueryTranslator,
)
from app.stocks.wiring import (
    analysis_cache_ttl,
    bedrock_recovery_model_id,
    get_provider,
)

@lru_cache(maxsize=1)
def get_etf_screener_translator() -> EtfScreenerQueryTranslator:
    # The ETF sibling of the stock screener's translator: the AI ETF screener's translation is its
    # primary data, so it's required, but there's no secret to gate on (Bedrock authenticates
    # through the process's AWS credentials — the ECS task role in prod). It shares the stock
    # screener's env so one config drives both: BEDROCK_REGION (default us-east-1) and the optional
    # BEDROCK_SCREENER_MODEL_ID (a cross-region inference profile). A missing 'bedrock' extra
    # surfaces as a clean 503 here rather than a 500.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_SCREENER_MODEL_ID")
    try:
        if model_id:
            return BedrockEtfScreenerQueryTranslator(model_id=model_id, region=region)
        return BedrockEtfScreenerQueryTranslator(region=region)
    except ImportError as exc:
        raise HTTPException(
            503, "AI ETF screening is not configured (install the 'bedrock' extra)."
        ) from exc


router = APIRouter(tags=["etfs"])


def get_search_use_case(db: Session = Depends(get_db)) -> SearchEtfs:
    # Pure DB read over the etfs table — no vendor, no key to gate on. The repository is
    # request-scoped, like the session.
    return SearchEtfs(SqlEtfSearchRepository(db))


def get_categories_use_case(db: Session = Depends(get_db)) -> ListEtfCategories:
    return ListEtfCategories(SqlEtfSearchRepository(db))


def get_ai_etf_search_use_case(
    db: Session = Depends(get_db),
    translator: EtfScreenerQueryTranslator = Depends(get_etf_screener_translator),
) -> AiScreenEtfs:
    # The AI screen only translates — it reads the stored set's categories (the translator's
    # allowed vocabulary) but does not run the search itself. The translator (Bedrock) is the only
    # non-DB dependency — it carries its own 503 gate in the wiring.
    return AiScreenEtfs(translator, SqlEtfSearchRepository(db))


def get_etf_detail_use_case(
    provider: StockQuoteProvider = Depends(get_provider),
    db: Session = Depends(get_db),
) -> GetEtfDetail:
    # The Alpaca singleton backs the live quote AND the opt-in trailing-return windows (the same
    # instance the quote/ticker endpoints use, so the fund's move never disagrees) — it implements
    # both StockQuoteProvider and StockPerformanceProvider, so the one dependency serves both roles
    # (the isinstance mirrors the ticker card's wiring). The lookup repository is the request-scoped
    # read over the etfs table + its profile children (the membership gate, the stored facts, and
    # the stored profile). The Yahoo profile provider backs the performance block's live 3y/5y
    # returns (no longer stored) — the one live Yahoo call on the read path, made only when that
    # block is requested; it's keyless, so it's always constructable (best-effort like the windows).
    performance = provider if isinstance(provider, StockPerformanceProvider) else None
    return GetEtfDetail(
        SqlEtfLookupRepository(db), provider, performance, YfinanceEtfProfileProvider()
    )


@lru_cache(maxsize=1)
def get_etf_analysis_provider() -> EtfAnalysisProvider:
    # The Bedrock analyser, a process singleton (the SDK client is reusable and the config is
    # static). Shares the stock analyser's env, so one deploy config drives both: BEDROCK_REGION
    # (default us-east-1) and the optional BEDROCK_ANALYSIS_MODEL_ID (a cross-region inference
    # profile). There is no API key — Bedrock authenticates through the process's AWS credentials
    # (the ECS task role in prod). The anthropic/bedrock extra is optional and imported lazily, so a
    # deploy without it turns the ImportError into a 503 here rather than failing app import.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_ANALYSIS_MODEL_ID")
    # Shares the stock analyser's escalation env (BEDROCK_ANALYSIS_RECOVERY_MODEL_ID),
    # like the primary model above — one deploy config drives both. Unset → no escalation.
    recovery = bedrock_recovery_model_id("BEDROCK_ANALYSIS_RECOVERY_MODEL_ID")
    try:
        if model_id:
            return BedrockEtfAnalysisProvider(
                model_id=model_id, region=region, recovery_model_id=recovery
            )
        return BedrockEtfAnalysisProvider(region=region, recovery_model_id=recovery)
    except ImportError as exc:
        raise HTTPException(
            503, "AI analysis is not configured (install the 'bedrock' extra)."
        ) from exc


def get_etf_analysis_cache(
    db: Session = Depends(get_db),
) -> InvestmentAnalysisCache:
    # The read-through result cache for the fund analysis (kind="etf", so it never collides with a
    # stock of the same ticker). Same table + best-effort contract as the stock analysis cache.
    return SqlInvestmentAnalysisCache(db, "etf")


def get_etf_analysis_use_case(
    detail: GetEtfDetail = Depends(get_etf_detail_use_case),
    analyzer: EtfAnalysisProvider = Depends(get_etf_analysis_provider),
    cache: InvestmentAnalysisCache = Depends(get_etf_analysis_cache),
) -> GetEtfAnalysis:
    # Reuses the detail use case as the primary snapshot builder (so the analysis reasons over
    # exactly what the detail card shows — same quote, same stored facts, same profile) and pairs it
    # with the Bedrock analyser + the read-through result cache (a fresh stored read skips the
    # snapshot build and the model call). The detail's missing-keys 503 (Alpaca quote) and the
    # analyser's 503 (missing extra) both ride through. TTL is the "etf" kind's (profile is slow,
    # only the quote is live), overridable via ANALYSIS_CACHE_TTL_MINUTES_ETF.
    return GetEtfAnalysis(detail, analyzer, cache=cache, cache_ttl=analysis_cache_ttl("etf"))


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
                dividend_yield=r.dividend_yield,
            )
            for r in page.results
        ],
    )


def _present_categories(categories: EtfCategories) -> EtfCategoriesResponse:
    """Presenter: categories entity -> HTTP response DTO."""
    return EtfCategoriesResponse(categories=list(categories.categories))


def _present_ai_etf_screen(intent: EtfScreenIntent) -> AiEtfScreenResponse:
    """Presenter: the AI's EtfScreenIntent -> HTTP response DTO (the interpreted filters)."""
    return AiEtfScreenResponse(
        interpreted=AiEtfScreenInterpretationResponse(
            query=intent.query,
            categories=list(intent.categories),
            sort=intent.sort.value if intent.sort is not None else None,
            direction=intent.direction.value,
            limit=intent.limit,
        ),
    )


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
    blocked) plus Yahoo's 3y/5y annualized returns — the latter overlaid onto the profile by the
    use case from a live Yahoo read (no longer stored), likewise null when that read was blocked."""
    perf = detail.performance
    p = detail.profile
    return EtfPerformanceResponse(
        one_week=perf.one_week if perf else None,
        one_month=perf.one_month if perf else None,
        three_month=perf.three_month if perf else None,
        six_month=perf.six_month if perf else None,
        ytd=perf.ytd if perf else None,
        one_year=perf.one_year if perf else None,
        three_year=p.three_year_return,
        five_year=p.five_year_return,
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


# Authored by the service and attached at the edge — never trusted to the model — so the legal
# framing is ours, not something the language model can drop or reword. The same disclaimer the
# stock analysis serves, since the caveat is identical for a fund.
_ANALYSIS_DISCLAIMER = (
    "AI-generated for informational and educational purposes only — not financial "
    "advice. Markets carry risk; do your own research before investing."
)


def _present_etf_analysis(analysis: InvestmentAnalysis) -> EtfAnalysisResponse:
    """Presenter: the AI analysis entity -> HTTP response DTO.

    Maps the entity's ``symbol`` onto the ETF slice's ``ticker`` field, unpacks the enums to their
    string values, turns the strengths/risks tuples into lists, and attaches the service-authored
    disclaimer (the model never sees or controls it)."""
    return EtfAnalysisResponse(
        ticker=analysis.symbol,
        recommendation=analysis.recommendation.value,
        confidence=analysis.confidence.value,
        thesis=analysis.thesis,
        strengths=list(analysis.strengths),
        risks=list(analysis.risks),
        disclaimer=_ANALYSIS_DISCLAIMER,
        model=analysis.model,
        generated_at=analysis.generated_at,
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
    category: list[str] | None = Query(
        None,
        description=(
            "Filter by fund category (the ETF type). Repeat to match several at once "
            "(?category=large_growth&category=large_blend — an OR set). Each accepts the slug from "
            "/stocks/etfs/categories (e.g. 'large_growth') or the raw label ('Large Growth'). "
            "Omit for every category."
        ),
    ),
    sort: EtfSort = Query(
        EtfSort.NET_ASSETS,
        description=(
            "Sort field: net_assets (assets under management, default — the biggest/top funds), "
            "expense_ratio (pair with order=asc for cheapest first), or dividend_yield "
            "(highest-income first with the default order=desc)."
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
            query=q,
            categories=category,
            sort=sort,
            direction=order,
            limit=limit,
            offset=offset,
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


@router.get("/stocks/etfs/ai-search", response_model=AiEtfScreenResponse)
def ai_search_etfs_endpoint(
    response: Response,
    q: str = Query(
        ...,
        min_length=1,
        description=(
            "A plain-English ETF-screen request — e.g. 'cheap S&P 500 index funds', 'high-yield "
            "dividend ETFs', or 'gold funds by size'. An AI translates it into the same filters "
            "the manual /stocks/etfs search accepts and returns just those interpreted filters (it "
            "does not run the search) — the client applies them to /stocks/etfs to fetch the rows, "
            "so it can show and edit them."
        ),
    ),
    use_case: AiScreenEtfs = Depends(get_ai_etf_search_use_case),
) -> AiEtfScreenResponse:
    """Translate a natural-language request into ETF-screen filters. A blank request is a 400; a
    translation failure (the model/vendor couldn't parse it) is a 502."""
    try:
        intent = use_case.execute(query=q)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(
            502, "AI ETF screening is temporarily unavailable."
        ) from exc
    # Deterministic for a given request against the slow-moving set — cache briefly like the manual
    # search so a burst of identical queries collapses onto one translation.
    response.headers["Cache-Control"] = "public, max-age=60"
    return _present_ai_etf_screen(intent)


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


@router.get("/stocks/etf/{ticker}/analysis", response_model=EtfAnalysisResponse)
def get_etf_analysis_endpoint(
    ticker: str,
    response: Response,
    use_case: GetEtfAnalysis = Depends(get_etf_analysis_use_case),
) -> EtfAnalysisResponse:
    """A plain-language, AI-generated buy/hold/sell read on one fund — the ETF sibling of
    ``GET /stocks/{symbol}/analysis``. Builds the fund's snapshot (the same quote + stored facts +
    profile the detail card shows, plus the trailing/long-term returns) and asks Claude on Bedrock
    for a balanced read grounded only in those figures.

    Same error map as the detail card: a bad ticker is a 400, a non-ETF a 404, and a failed primary
    (the live quote) or a failed model call a 502. A missing bedrock extra is a 503 from the wiring.
    """
    try:
        analysis = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        # Not in the stored ETF universe (or a symbol with no data) -> "not an ETF".
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        # The primary snapshot (the live quote) or the model call failed.
        raise HTTPException(502, str(exc)) from exc
    # Model calls are slow and metered, and a fund's fundamentals move slowly — cache briefly (the
    # same 5 min the stock analysis and the detail card use) so a burst of viewers collapses onto one
    # generation.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present_etf_analysis(analysis)
