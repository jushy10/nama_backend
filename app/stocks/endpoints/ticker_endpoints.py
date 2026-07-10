"""HTTP API for the ticker resource — the single-stock card plus the universe search.

``GET /stocks/ticker/{ticker}`` — the read endpoint for the ticker slice: the live
quote (price + day move), the clean company name, and the anchor facts served straight
from the ``stocks`` row — the listing exchange (learned once from the price feed) plus
the universe screen's market cap, sector and industry — then **opt-in blocks**
requested via ``?include=`` — ``dividend``, ``performance`` (trailing windows),
``metrics`` (the trailing P/E — price over the quarterly slice's stored TTM EPS, on
the analyst-consensus basis — the margins off the fundamentals call, and the annual
slice's latest trailing YoY revenue/EPS growth off the same anchor read), and
``options_metrics`` (the **options-market read**: ATM implied volatility, the priced-in
expected move, the cost of a protective put, and the day's put/call lean — what the
options market believes about the stock, for a buyer sizing an entry). Pay-per-use: a
block that isn't requested costs no provider call — and market cap now riding the
anchor, the fundamentals call is itself opt-in (only ``dividend``/``metrics`` pull it).
Controller + presenter + wiring, the composition-root way, sitting in
``app/stocks/endpoints/`` like the other slices' HTTP.

Beside the card's *item* route live its *collection* and *filter menus*, reading the
**universe slice** off the same ``stocks`` anchor (grouped here because they share the
``/stocks/ticker`` resource, not the ticker slice's internals):

- ``GET /stocks/ticker`` — a paginated search/filter/sort over the screened universe: a
  free-text ``q`` matched case-insensitively against name *or* ticker (so "NV" surfaces
  Nvidia and NVDA), ``sector``/``industry`` slug filters, the ``in_sp500``/``in_nasdaq100``
  membership flags, a ``market_cap`` tier filter (mega/large/mid/small), and a ``sort`` (omit
  for an unsorted A→Z by ticker, else market cap, trailing revenue/EPS growth or their blend,
  the forward FY1→FY2 consensus counterparts, or trailing P/E) with an ``order``. Rows are DB
  facts only — no live price; a
  client opens ``{ticker}`` above for the live card. Pure DB read
  (``SqlStockSearchRepository`` → ``SearchStocks``), no vendor or key, so the only request
  error is a 400 (a bad ``sort``/``order`` is a 422 from the enum binding).
- ``GET /stocks/classifications`` — the distinct sector + industry slugs, for the FE's
  filter menus (``ListClassifications``).

Wiring convention: this endpoint owns no vendor of its own — it reuses the composition
root's factories. The quote and performance windows ride the ``@lru_cache``d Alpaca
provider (whose missing-keys 503 gate the endpoint inherits: the quote is primary
here), the name and fundamentals ride the optional Finnhub providers (best-effort,
``None`` without a key), the estimates ride the annual-earnings projection
(DB-only, no key), the trailing P/E's TTM sum rides the quarterly-earnings
slice's read-through DB cache (keyless; live to Yahoo only on a cold miss,
best-effort), and the options chain rides Yahoo via yfinance (keyless,
best-effort even when requested — Yahoo intermittently blocks data-centre IPs, and
a missing insight must not take the quote down). There's no cron or table behind
this endpoint: the card is built around the live quote, so it's computed per
request — freshness of the consensus legs is the annual-earnings slice's job
(lazy fill + its sync cron).
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.yfinance_eps_history_adapter import YfinanceEpsHistoryProvider
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.endpoints.quarterly_earnings_endpoints import (
    get_quarterly_earnings_provider,
)
from app.stocks.entities import StockPerformance
from app.stocks.etfs.db_repository import SqlEtfLookupRepository
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import (
    CompanyProfileProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
)
from app.stocks.router import (
    get_fundamentals_provider,
    get_options_provider,
    get_profile_provider,
    get_provider,
    get_screener_translator,
)
from app.stocks.schemas import StockPerformanceResponse
from app.stocks.ticker.db_repository import SqlTickerRepository
from app.stocks.ticker.entities import PeHistory, PeHistoryStats, TickerOptionsMetrics
from app.stocks.ticker.ports import OptionChainProvider
from app.stocks.ticker.schemas import (
    DividendResponse,
    OptionsMetricsResponse,
    PeHistoryPointResponse,
    PeHistoryResponse,
    PeHistoryStatsResponse,
    TickerCardResponse,
    TickerMetricsResponse,
    TickerTypeResponse,
)
from app.stocks.ticker.use_cases import (
    ClassifyTicker,
    GetStockPeHistory,
    GetTickerCard,
    TickerCard,
)
from app.stocks.universe.db_repository import SqlStockSearchRepository
from app.stocks.universe.entities import (
    Classifications,
    IndustryValuation,
    MarketCapTier,
    ScreenIntent,
    SortDirection,
    StockSearchPage,
    StockSort,
)
from app.stocks.universe.ports import ScreenerQueryTranslator
from app.stocks.universe.schemas import (
    AiScreenInterpretationResponse,
    AiScreenResponse,
    ClassificationsResponse,
    IndustryValuationResponse,
    StockSearchItemResponse,
    StockSearchResponse,
)
from app.stocks.universe.use_cases import (
    AiScreenStocks,
    GetIndustryValuation,
    ListClassifications,
    SearchStocks,
)

router = APIRouter(tags=["ticker"])


def get_ticker_card_use_case(
    provider=Depends(get_provider),
    fundamentals: StockFundamentalsProvider | None = Depends(get_fundamentals_provider),
    profile: CompanyProfileProvider | None = Depends(get_profile_provider),
    options: OptionChainProvider = Depends(get_options_provider),
    earnings: QuarterlyEarningsProvider = Depends(get_quarterly_earnings_provider),
    db: Session = Depends(get_db),
) -> GetTickerCard:
    # The Alpaca singleton backs the quote, the trailing performance windows, and the
    # one-time exchange lookup (the same instance every other price view uses). The
    # profile provider supplies the display name (the slim quote carries none),
    # TTL-cached like on the snapshot; the repository serves the stored exchange off
    # the stocks row. The options chain is the keyless yfinance singleton — always
    # wired, best-effort at read — and the quarterly-earnings provider is the same DB
    # cache the earnings endpoint reads (lazy-filled on a miss, refreshed by its cron),
    # so the trailing P/E's TTM sum rides rows the earnings view already keeps warm.
    performance = provider if isinstance(provider, StockPerformanceProvider) else None
    return GetTickerCard(
        provider,
        fundamentals,
        performance,
        profile,
        stocks=provider,
        repository=SqlTickerRepository(db),
        options=options,
        earnings=earnings,
        # The card's asset_type is a single indexed membership check against the etfs
        # table (same request-scoped session as the anchor read) — "etf" for a screened
        # fund, else "equity".
        etfs=SqlEtfLookupRepository(db),
    )


def _round2(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _present_performance(
    perf: StockPerformance | None,
) -> StockPerformanceResponse | None:
    if perf is None:
        return None
    return StockPerformanceResponse(
        one_week=perf.one_week,
        one_month=perf.one_month,
        three_month=perf.three_month,
        six_month=perf.six_month,
        ytd=perf.ytd,
        one_year=perf.one_year,
    )


def _present_options_metrics(
    metrics: TickerOptionsMetrics | None,
) -> OptionsMetricsResponse | None:
    if metrics is None:
        return None
    # Rounded here at the edge like the dividend: these are display figures
    # (percents, a ratio) and the chain arithmetic carries float noise.
    return OptionsMetricsResponse(
        implied_volatility=_round2(metrics.implied_volatility),
        expected_move_percent=_round2(metrics.expected_move_percent),
        expected_move_by=metrics.expected_move_by,
        insurance_cost_percent=_round2(metrics.insurance_cost_percent),
        insurance_expires=metrics.insurance_expires,
        put_call_ratio=_round2(metrics.put_call_ratio),
    )


def _present(card: TickerCard) -> TickerCardResponse:
    """Presenter: ticker-card composition -> HTTP response DTO.

    The domain speaks in ``symbol``; renaming it ``ticker`` is a JSON-shape choice
    made here at the edge, like the DTOs' other shape concerns. Opt-in blocks are
    emitted only when the card was asked to carry them — ``card.include`` gates the
    dividend block and the metrics' fundamentals-backed half (which is ``None`` when
    neither was requested, since fundamentals is only fetched for those); performance
    is already ``None`` when unrequested. Market cap, sector and industry ride the
    anchor read, so they're always served (``null`` until the row carries them)."""
    fundamentals = card.fundamentals
    dividend = None
    if "dividend" in card.include and fundamentals is not None:
        # Rounded here at the edge: a dividend card shows cents / basis-point-ish
        # precision, and the vendor's raw figures carry float noise. The shared
        # entity stays unrounded — the snapshot serves the same fields raw.
        dividend = DividendResponse(
            yield_percentage=_round2(fundamentals.dividend_yield),
            per_share=_round2(fundamentals.dividend_per_share),
        )
    metrics = None
    if "metrics" in card.include:
        # The trailing P/E rides the valuation (the quarterly slice's TTM sum on the
        # adjusted EPS basis, deliberately NOT the vendor's GAAP-ish TTM read); the
        # margins ride the fundamentals call.
        trailing = fundamentals.metrics if fundamentals else None
        metrics = TickerMetricsResponse(
            pe=card.valuation.trailing_pe if card.valuation else None,
            # The FCF/OCF reads ride the valuation too (live price / the annual slice's
            # stored per-share cash off the anchor), so they're on the same live quote as
            # the P/E — and, unlike the margins, independent of the fundamentals call.
            price_to_fcf=card.valuation.price_to_fcf if card.valuation else None,
            fcf_yield=card.valuation.fcf_yield if card.valuation else None,
            ocf_yield=card.valuation.ocf_yield if card.valuation else None,
            gross_margin=trailing.gross_margin if trailing else None,
            operating_margin=trailing.operating_margin if trailing else None,
            net_margin=trailing.net_margin if trailing else None,
            # The trailing YoY figures ride the anchor read (already rounded percent),
            # not the fundamentals call — so they serve even when Finnhub is down.
            revenue_growth_yoy=card.revenue_growth_yoy,
            eps_growth_yoy=card.eps_growth_yoy,
            fcf_growth_yoy=card.fcf_growth_yoy,
        )
    return TickerCardResponse(
        ticker=card.quote.symbol,
        name=card.name,
        exchange=card.exchange,
        asset_type=card.asset_type,
        price=card.quote.price,
        change=card.quote.change,
        change_percent=card.quote.change_percent,
        market_cap=card.market_cap,
        sector=card.sector,
        industry=card.industry,
        dividend=dividend,
        performance=_present_performance(card.performance),
        metrics=metrics,
        options_metrics=_present_options_metrics(card.options_metrics),
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
        card = use_case.execute(ticker, include=include)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # A valuation card, not a ticking price: the consensus legs move on analyst
    # revisions and the multiple doesn't need tick precision, so cache briefly —
    # a burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(card)


@lru_cache
def _eps_history_provider() -> YfinanceEpsHistoryProvider:
    # Keyless yfinance singleton (like the options provider): it shares the module-level
    # pacing state and is best-effort at read, so it's always constructable — no key gate.
    return YfinanceEpsHistoryProvider()


def get_pe_history_use_case(
    provider=Depends(get_provider),
) -> GetStockPeHistory:
    # The Alpaca singleton supplies the daily closes (it implements CandleProvider — the
    # same instance the candle chart uses), and the deep reported-EPS history rides the
    # keyless yfinance adapter. The card's Alpaca 503 gate is inherited (the closes are
    # primary here); the EPS leg is best-effort, so no extra key to gate on.
    return GetStockPeHistory(provider, _eps_history_provider())


def _present_pe_stats(stats: PeHistoryStats | None) -> PeHistoryStatsResponse | None:
    """Presenter: the P/E-history valuation summary -> DTO. The entity already rounds every
    figure (the percentiles, median/quartiles, the discount), so this just maps fields and
    renders the signal enum as its string value. ``None`` passes through — a series too short
    for a stable percentile carries no stats block."""
    if stats is None:
        return None
    return PeHistoryStatsResponse(
        current_pe=stats.current_pe,
        median_pe=stats.median_pe,
        p25_pe=stats.p25_pe,
        p75_pe=stats.p75_pe,
        min_pe=stats.min_pe,
        max_pe=stats.max_pe,
        current_percentile=stats.current_percentile,
        discount_to_median_percent=stats.discount_to_median_percent,
        signal=stats.signal.value,
        sample_size=stats.sample_size,
    )


def _present_pe_history(history: PeHistory) -> PeHistoryResponse:
    """Presenter: P/E-history entity -> HTTP response DTO. Rounds the display figures at
    the edge (``pe`` is already 2-dp from the entity; price/EPS carry feed float noise), and
    attaches the valuation-vs-history ``stats`` (``None`` for a series too short to rank)."""
    return PeHistoryResponse(
        ticker=history.symbol,
        count=len(history.points),
        points=[
            PeHistoryPointResponse(
                date=point.report_date,
                price=round(point.price, 2),
                ttm_eps=round(point.ttm_eps, 2),
                pe=point.pe,
            )
            for point in history.points
        ],
        stats=_present_pe_stats(history.stats),
    )


@router.get(
    "/stocks/ticker/{ticker}/pe-history", response_model=PeHistoryResponse
)
def get_pe_history_endpoint(
    ticker: str,
    response: Response,
    use_case: GetStockPeHistory = Depends(get_pe_history_use_case),
) -> PeHistoryResponse:
    """A stock's trailing P/E sampled at each earnings release (oldest first): the close
    on each announcement date over the trailing-twelve-month reported EPS then known. The
    backward-looking companion to the card's live ``metrics.pe`` — how the multiple has
    moved over time. The EPS leg is best-effort (keyless Yahoo, which blocks data-centre
    IPs intermittently), so an uncovered or blocked symbol is a 200 with an empty
    ``points``, never a 404/502; only the Alpaca price leg can raise."""
    try:
        history = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # A historical series that only extends when a new quarter reports (its latest point
    # tailed by today's close) — cache an hour, like the other slow-moving card reads.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return _present_pe_history(history)


def get_classify_ticker_use_case(db: Session = Depends(get_db)) -> ClassifyTicker:
    # Pure DB read: a single indexed membership check against the etfs table — no
    # vendor, no key, request-scoped session — so it's always constructable.
    return ClassifyTicker(SqlEtfLookupRepository(db))


@router.get("/stocks/type/{ticker}", response_model=TickerTypeResponse)
def get_ticker_type_endpoint(
    ticker: str,
    response: Response,
    use_case: ClassifyTicker = Depends(get_classify_ticker_use_case),
) -> TickerTypeResponse:
    """Classify a ticker as an ETF or an equity — the cheap, quote-free counterpart
    to the ticker card's ``asset_type`` (one indexed ETF-universe membership check)."""
    try:
        result = use_case.classify(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # ETF-universe membership only shifts when the screen re-syncs (rare), so cache
    # generously — a burst of classifier calls collapses onto one read.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return TickerTypeResponse(ticker=result.ticker, asset_type=result.asset_type)


# --- The universe search + filter menus (the /stocks/ticker collection) -------------------
#
# The read side of the universe slice, grouped here beside the card because they share the
# /stocks/ticker resource. Both read only the shared `stocks` anchor — no vendor, no key — so
# the use cases are always constructable.


def get_search_use_case(db: Session = Depends(get_db)) -> SearchStocks:
    # Pure DB read over the shared anchor — no vendor, no key to gate on. The repository is
    # request-scoped, like the session.
    return SearchStocks(SqlStockSearchRepository(db))


def get_classifications_use_case(db: Session = Depends(get_db)) -> ListClassifications:
    return ListClassifications(SqlStockSearchRepository(db))


def get_industry_valuation_use_case(
    db: Session = Depends(get_db),
) -> GetIndustryValuation:
    # Same request-scoped read repository the search uses — a pure DB read over the anchor.
    return GetIndustryValuation(SqlStockSearchRepository(db))


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
                pe_ratio=r.pe_ratio,
                fcf_yield=r.fcf_yield,
                revenue_growth_yoy=r.revenue_growth_yoy,
                eps_growth_yoy=r.eps_growth_yoy,
                fcf_growth_yoy=r.fcf_growth_yoy,
                forward_revenue_growth_yoy=r.forward_revenue_growth_yoy,
                forward_eps_growth_yoy=r.forward_eps_growth_yoy,
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


def _present_industry_valuation(
    v: IndustryValuation,
) -> IndustryValuationResponse:
    """Presenter: industry-valuation entity -> HTTP response DTO."""
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
            "forward_revenue_growth, forward_eps_growth, forward_growth; or pe (trailing P/E on "
            "the consensus basis; ascending surfaces the cheapest on earnings). Stocks missing "
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
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # The universe is slow-moving (refreshed out of band by the sync cron) and this is a plain
    # DB read — cache briefly so a burst of viewers (and any CDN in front) collapses onto one
    # query without going stale.
    response.headers["Cache-Control"] = "public, max-age=60"
    return _present_search(page)


def get_ai_search_use_case(
    db: Session = Depends(get_db),
    translator: ScreenerQueryTranslator = Depends(get_screener_translator),
) -> AiScreenStocks:
    # The AI screen only translates — it reads the universe's classifications (the
    # translator's allowed vocabulary) but does not run the search itself. The translator
    # (Bedrock) is the only non-DB dependency — it carries its own 503 gate in the wiring.
    return AiScreenStocks(translator, SqlStockSearchRepository(db))


def _present_ai_screen(intent: ScreenIntent) -> AiScreenResponse:
    """Presenter: the AI's ScreenIntent -> HTTP response DTO (the interpreted filters)."""
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
    """Translate a natural-language request into screen filters. A blank request is a 400; a
    translation failure (the model/vendor couldn't parse it) is a 502."""
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
    """The trailing-P/E benchmark for one industry — the median plus the interquartile range
    of its **mid-cap-and-up** peers' P/Es (a $2B market-cap floor drops the noisy $1–2B
    tail), for judging whether a stock's multiple is rich or cheap for its industry.
    ``industry`` accepts the slug from /stocks/classifications (e.g. 'semiconductors') or
    the raw label. An unknown industry is a 200 with count 0, not a 404."""
    try:
        valuation = use_case.execute(industry)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # A plain DB read over the slow-moving universe (refreshed out of band by the sync) — cache
    # briefly like the search list so a burst of viewers collapses onto one query.
    response.headers["Cache-Control"] = "public, max-age=60"
    return _present_industry_valuation(valuation)
