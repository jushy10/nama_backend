import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.rate_limit import limiter
from app.stocks.adapters.bedrock.bedrock_analysis_adapter import BedrockScorecardProvider
from app.stocks.adapters.bedrock.bedrock_earnings_analysis_adapter import (
    BedrockEarningsAnalysisProvider,
)
from app.stocks.adapters.bedrock.bedrock_fundamentals_analysis_adapter import (
    BedrockFundamentalsAnalysisProvider,
)
from app.stocks.adapters.bedrock.bedrock_market_summary_adapter import (
    BedrockMarketSummaryProvider,
)
from app.stocks.adapters.bedrock.bedrock_ratings_analysis_adapter import (
    BedrockRatingsAnalysisProvider,
)
from app.stocks.adapters.bedrock.bedrock_sector_analysis_adapter import (
    BedrockSectorAnalysisProvider,
)
from app.stocks.adapters.db.db_only_context_providers import (
    DbOnlyAnnualEarningsProvider,
    DbOnlyQuarterlyEarningsProvider,
    DbOnlyRatingChangesProvider,
    DbOnlyRecommendationsProvider,
)
from app.stocks.adapters.yfinance.eps_history_adapter import YfinanceEpsHistoryProvider
from app.stocks.ai.analysis.db_ai_analysis_cache import (
    earnings_analysis_cache,
    fundamentals_analysis_cache,
    market_summary_cache,
    ratings_analysis_cache,
    sector_analysis_cache,
)
from app.stocks.ai.analysis.entities import (
    EarningsAnalysis,
    FundamentalsAnalysis,
    MarketIndexReturn,
    MarketPeriodHighlight,
    MarketSummary,
    RatingsAnalysis,
    SectorAnalysis,
    SectorHeadline,
    SectorHighlight,
    SectorMover,
    StockScorecard,
)
from app.stocks.ai.analysis.interfaces import (
    EarningsAnalysisProvider,
    FundamentalsAnalysisProvider,
    MarketSummaryProvider,
    RatingsAnalysisProvider,
    SectorAnalysisProvider,
    StockScorecardCache,
    StockScorecardProvider,
)
from app.stocks.ai.analysis.db_stock_scorecard_cache import SqlStockScorecardCache
from app.stocks.ai.analysis.schemas import (
    EarningsAnalysisResponse,
    FundamentalsAnalysisResponse,
    InvestmentAnalysisResponse,
    MarketIndexReturnResponse,
    MarketPeriodResponse,
    MarketSummaryResponse,
    RatingsAnalysisResponse,
    ScorecardSectionResponse,
    SectionMetricResponse,
    SectorAnalysisResponse,
    SectorHeadlineResponse,
    SectorHighlightResponse,
    SectorMoverResponse,
)
from app.stocks.ai.analysis.use_cases import (
    GetEarningsAnalysis,
    GetFundamentalsAnalysis,
    GetMarketSummary,
    GetRatingsFindings,
    GetSectorAnalysis,
    GetStockAnalysis,
    GetStockInfo,
)
from app.stocks.company.ticker.use_cases import GetStockPeHistory
from app.stocks.company.earnings.annual.db_repository import SqlAnnualEarningsRepository
from app.stocks.company.earnings.quarterly.db_repository import (
    SqlQuarterlyEarningsRepository,
)
from app.stocks.endpoints.market_endpoints import (
    get_market_overview,
    get_sector_performance,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.market.boards.use_cases import GetMarketOverview, GetSectorPerformance
from app.stocks.company.news.db_repository import SqlNewsRepository
from app.stocks.ports import (
    AllTimeHighProvider,
    AnalystEstimatesProvider,
    StockDataProvider,
    StockPerformanceProvider,
)
from app.stocks.company.recommendations.db_repository import (
    SqlRatingChangesRepository,
    SqlRecommendationsRepository,
)
from app.stocks.catalog.universe.db_repository import SqlStockSearchRepository
from app.stocks.wiring import (
    analysis_cache_ttl,
    bedrock_recovery_model_id,
    get_estimates_provider,
    get_price_provider,
    get_provider,
)

router = APIRouter(tags=["stocks"])

# A tight per-IP limit on the AI reads, layered on top of the app-wide default
# limits (app/rate_limit.py). These routes each make a metered Bedrock call on a
# cache miss (~$0.005), so the generic 20/s + 600/min per-IP allowance — sized for
# cheap DB reads — is far too loose here: it lets one IP enumerate distinct symbols
# and rack up model spend. The result cache stops *repeat* views of the same symbol
# from re-billing; this stops a single IP from forcing many *distinct*-symbol misses.
# Each decorated endpoint gets its **own** bucket (SlowAPI scopes by view function),
# so the six reads don't share one allowance. Env-tunable so it can be tightened or
# loosened without a deploy; the default is generous for a human browsing distinct
# tickers but kills a scraping loop.
_AI_ANALYSIS_RATE_LIMIT = os.environ.get("AI_ANALYSIS_RATE_LIMIT", "10/minute")


def get_stock_info(
    provider: StockDataProvider = Depends(get_price_provider),
    estimates: AnalystEstimatesProvider | None = Depends(get_estimates_provider),
) -> GetStockInfo:
    # The enriched snapshot use case now serves only as the AI analysis context
    # (the standalone GET /stocks/{symbol} endpoint was removed). The market-routing
    # provider supplies the snapshot, the performance windows, and the all-time high —
    # all derived from the same price feed, one instance backing each capability via its
    # respective port — routed per symbol (US→Alpaca / CA→Yahoo), so a Canadian ticker's
    # analysis context reads its price from Yahoo. The router implements AllTimeHighProvider
    # too, so this keeps the drawdown-from-high context for US symbols (a router missing it
    # would drop it for everyone). The trailing fundamentals + clean name are no longer read
    # from a live vendor here — the analysis use cases overlay them from the stocks anchor
    # (materialized by the fundamentals/universe syncs).
    performance = provider if isinstance(provider, StockPerformanceProvider) else None
    all_time_high = provider if isinstance(provider, AllTimeHighProvider) else None
    return GetStockInfo(provider, performance, all_time_high, estimates)


@lru_cache(maxsize=1)
def get_analysis_provider() -> StockScorecardProvider:
    # AI analysis is this endpoint's primary data, so it's required — but unlike
    # the API-key vendors there's no secret to gate on: Bedrock authenticates
    # through the process's AWS credentials (the ECS task role in production), so
    # the IAM policy is what enables it. Region + model id are config with sane
    # defaults (the model id may be a cross-region inference profile). A missing
    # 'anthropic' Bedrock extra surfaces as a clean 503 here rather than a 500.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_ANALYSIS_MODEL_ID")
    # The single incomplete-result retry escalates onto this model when set (else it
    # stays on the primary) — see wiring.bedrock_recovery_model_id.
    recovery = bedrock_recovery_model_id("BEDROCK_ANALYSIS_RECOVERY_MODEL_ID")
    try:
        if model_id:
            return BedrockScorecardProvider(
                model_id=model_id, region=region, recovery_model_id=recovery
            )
        return BedrockScorecardProvider(region=region, recovery_model_id=recovery)
    except ImportError as exc:
        raise HTTPException(
            503, "AI analysis is not configured (install the 'bedrock' extra)."
        ) from exc


def get_analysis_cache(
    db: Session = Depends(get_db),
) -> StockScorecardCache:
    # The read-through result cache for the stock scorecard (kind="stock", so it
    # never collides with a fund of the same ticker). One row per symbol, refreshed
    # whenever a served read ages past the use case's TTL — best-effort, so a DB
    # problem degrades to a regeneration, never an error.
    return SqlStockScorecardCache(db, "stock")


def get_stock_analysis(
    stock_info: GetStockInfo = Depends(get_stock_info),
    analyzer: StockScorecardProvider = Depends(get_analysis_provider),
    cache: StockScorecardCache = Depends(get_analysis_cache),
    # Best-effort *context* for the analysis: the quarterly and annual earnings
    # timelines and the analyst recommendation trends. Read **DB-only** here (via the
    # slices' repositories, not their read-through providers) — this path must never
    # trigger a synchronous, rate-limited Yahoo fetch on a cache miss, which would add
    # seconds to the request; keeping the caches current is the crons' job. An
    # uncovered symbol simply omits the block.
    db: Session = Depends(get_db),
) -> GetStockAnalysis:
    # Reuses the stock snapshot wiring wholesale (price + enrichment), then layers the
    # analyzer, the DB-only earnings + recommendations context, the industry-P/E
    # benchmark (a pure DB read off the shared anchor — the same screened universe the
    # /stocks/industries/{industry}/pe endpoint groups on), and the result cache.
    return GetStockAnalysis(
        stock_info,
        analyzer,
        DbOnlyQuarterlyEarningsProvider(SqlQuarterlyEarningsRepository(db)),
        DbOnlyAnnualEarningsProvider(SqlAnnualEarningsRepository(db)),
        DbOnlyRecommendationsProvider(SqlRecommendationsRepository(db)),
        SqlStockSearchRepository(db),
        cache=cache,
        cache_ttl=analysis_cache_ttl("stock"),
    )


@lru_cache(maxsize=1)
def get_sector_analysis_provider() -> SectorAnalysisProvider:
    # The sector read is short, plain output (a few sentences + two brief highlight
    # lists), so it runs on the fast Haiku tier — the provider's own default —
    # rather than inheriting BEDROCK_ANALYSIS_MODEL_ID (the per-stock and ETF
    # analysis's shared var). It gets its own override,
    # BEDROCK_SECTOR_ANALYSIS_MODEL_ID, so the model can still be swapped without a
    # code change. Bedrock authenticates through the process's AWS credentials, so
    # there's no secret to gate on; a missing 'bedrock' extra surfaces as a 503.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_SECTOR_ANALYSIS_MODEL_ID")
    recovery = bedrock_recovery_model_id("BEDROCK_SECTOR_ANALYSIS_RECOVERY_MODEL_ID")
    try:
        if model_id:
            return BedrockSectorAnalysisProvider(
                model_id=model_id, region=region, recovery_model_id=recovery
            )
        return BedrockSectorAnalysisProvider(region=region, recovery_model_id=recovery)
    except ImportError as exc:
        raise HTTPException(
            503, "AI analysis is not configured (install the 'bedrock' extra)."
        ) from exc


def get_sector_analysis(
    # Reuses the sector-board wiring wholesale (the Alpaca-backed
    # GetSectorPerformance), then enriches each sector with the grounded drivers behind
    # its move — the S&P 500 constituents' day-change (the same two legs the heat map
    # uses: a DB read over the anchor + the Alpaca batched quote feed) and their recent
    # headlines (DB-only, like the per-stock analysis context) — before handing the
    # enriched board to the analyzer. All three attribution legs are best-effort (a
    # failure degrades to the plain board), fronted by the read-through result cache
    # (market-wide, so one stored read serves every viewer within the TTL and skips the
    # gather + model call).
    sectors: GetSectorPerformance = Depends(get_sector_performance),
    analyzer: SectorAnalysisProvider = Depends(get_sector_analysis_provider),
    provider: StockDataProvider = Depends(get_provider),
    db: Session = Depends(get_db),
) -> GetSectorAnalysis:
    return GetSectorAnalysis(
        sectors,
        analyzer,
        cache=sector_analysis_cache(db),
        cache_ttl=analysis_cache_ttl("sector"),
        constituents=SqlStockSearchRepository(db),
        quotes=provider,  # the Alpaca singleton also implements BulkQuoteProvider
        news=SqlNewsRepository(db),
    )


@lru_cache(maxsize=1)
def get_market_summary_provider() -> MarketSummaryProvider:
    # The market read is short, plain output (a few sentences + three brief period
    # notes), so it runs on the fast Haiku tier — the provider's own default —
    # rather than inheriting BEDROCK_ANALYSIS_MODEL_ID (the per-stock and ETF
    # analysis's shared var). It gets its own override, BEDROCK_MARKET_SUMMARY_MODEL_ID, so
    # the model can still be swapped without a code change, exactly like the sector
    # read. Bedrock authenticates through the process's AWS credentials, so there's
    # no secret to gate on; a missing 'bedrock' extra surfaces as a 503.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_MARKET_SUMMARY_MODEL_ID")
    recovery = bedrock_recovery_model_id("BEDROCK_MARKET_SUMMARY_RECOVERY_MODEL_ID")
    try:
        if model_id:
            return BedrockMarketSummaryProvider(
                model_id=model_id, region=region, recovery_model_id=recovery
            )
        return BedrockMarketSummaryProvider(region=region, recovery_model_id=recovery)
    except ImportError as exc:
        raise HTTPException(
            503, "AI analysis is not configured (install the 'bedrock' extra)."
        ) from exc


def get_market_summary(
    # Reuses the index-board wiring wholesale (the Alpaca-backed
    # GetMarketOverview), then hands the board to the analyzer — fronted by the
    # read-through result cache (market-wide, so one stored read serves every viewer
    # within the TTL and skips the gather + model call).
    overview: GetMarketOverview = Depends(get_market_overview),
    analyzer: MarketSummaryProvider = Depends(get_market_summary_provider),
    db: Session = Depends(get_db),
) -> GetMarketSummary:
    return GetMarketSummary(
        overview,
        analyzer,
        cache=market_summary_cache(db),
        cache_ttl=analysis_cache_ttl("market"),
    )


@lru_cache(maxsize=1)
def get_earnings_analysis_provider() -> EarningsAnalysisProvider:
    # The earnings read is short, plain output (a few sentences + a few
    # highlights), so it runs on the fast Haiku tier — the provider's own default
    # — rather than inheriting BEDROCK_ANALYSIS_MODEL_ID (the per-stock and ETF
    # analysis's shared var). It gets its own override, BEDROCK_EARNINGS_ANALYSIS_MODEL_ID,
    # so the model can still be swapped without a code change, exactly like the
    # sector and market reads. Bedrock authenticates through the process's AWS
    # credentials, so there's no secret to gate on; a missing 'bedrock' extra
    # surfaces as a 503.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_EARNINGS_ANALYSIS_MODEL_ID")
    recovery = bedrock_recovery_model_id("BEDROCK_EARNINGS_ANALYSIS_RECOVERY_MODEL_ID")
    try:
        if model_id:
            return BedrockEarningsAnalysisProvider(
                model_id=model_id, region=region, recovery_model_id=recovery
            )
        return BedrockEarningsAnalysisProvider(region=region, recovery_model_id=recovery)
    except ImportError as exc:
        raise HTTPException(
            503, "AI analysis is not configured (install the 'bedrock' extra)."
        ) from exc


def get_earnings_analysis(
    analyzer: EarningsAnalysisProvider = Depends(get_earnings_analysis_provider),
    # The earnings timelines, read **DB-only** (via the slices' repositories, not
    # their read-through providers) — this path must never trigger a synchronous,
    # rate-limited Yahoo fetch on a cache miss; keeping the caches current is the
    # crons' job. A symbol with nothing on file yields a 502 from the use case.
    db: Session = Depends(get_db),
) -> GetEarningsAnalysis:
    return GetEarningsAnalysis(
        analyzer,
        DbOnlyQuarterlyEarningsProvider(SqlQuarterlyEarningsRepository(db)),
        DbOnlyAnnualEarningsProvider(SqlAnnualEarningsRepository(db)),
        cache=earnings_analysis_cache(db),
        cache_ttl=analysis_cache_ttl("earnings"),
    )


@lru_cache(maxsize=1)
def get_ratings_analysis_provider() -> RatingsAnalysisProvider:
    # The analyst-coverage read is short, plain output (a few sentences + a few findings), so it
    # runs on the fast Haiku tier — the provider's own default — with its own override,
    # BEDROCK_RATINGS_ANALYSIS_MODEL_ID, so the model can be swapped without a code change,
    # exactly like the earnings and market reads. Bedrock authenticates through the process's
    # AWS credentials, so there's no secret to gate on; a missing 'bedrock' extra is a 503.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_RATINGS_ANALYSIS_MODEL_ID")
    try:
        if model_id:
            return BedrockRatingsAnalysisProvider(model_id=model_id, region=region)
        return BedrockRatingsAnalysisProvider(region=region)
    except ImportError as exc:
        raise HTTPException(
            503, "AI analysis is not configured (install the 'bedrock' extra)."
        ) from exc


def get_ratings_findings(
    analyzer: RatingsAnalysisProvider = Depends(get_ratings_analysis_provider),
    # The recommendation consensus + rating-change events, read **DB-only** (via the slice's
    # repositories, not their read-through providers) — this path must never trigger a
    # synchronous, rate-limited Yahoo fetch on a cache miss; keeping the caches current is the
    # crons' job. A symbol with no coverage on file yields a 502 from the use case.
    db: Session = Depends(get_db),
) -> GetRatingsFindings:
    return GetRatingsFindings(
        analyzer,
        DbOnlyRecommendationsProvider(SqlRecommendationsRepository(db)),
        DbOnlyRatingChangesProvider(SqlRatingChangesRepository(db)),
        cache=ratings_analysis_cache(db),
        cache_ttl=analysis_cache_ttl("ratings"),
    )


@lru_cache(maxsize=1)
def get_fundamentals_analysis_provider() -> FundamentalsAnalysisProvider:
    # The fundamentals read is short, plain output (a few sentences + a few findings), so it
    # runs on the fast Haiku tier — the provider's own default — with its own override,
    # BEDROCK_FUNDAMENTALS_ANALYSIS_MODEL_ID, so the model can be swapped without a code change,
    # exactly like the earnings and ratings reads. Bedrock authenticates through the process's
    # AWS credentials, so there's no secret to gate on; a missing 'bedrock' extra is a 503.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_FUNDAMENTALS_ANALYSIS_MODEL_ID")
    try:
        if model_id:
            return BedrockFundamentalsAnalysisProvider(model_id=model_id, region=region)
        return BedrockFundamentalsAnalysisProvider(region=region)
    except ImportError as exc:
        raise HTTPException(
            503, "AI analysis is not configured (install the 'bedrock' extra)."
        ) from exc


@lru_cache(maxsize=1)
def _eps_history_provider() -> YfinanceEpsHistoryProvider:
    # Keyless yfinance singleton (like the options provider): shares the module-level pacing
    # state and is best-effort at read, so it's always constructable — no key to gate on. Backs
    # the fundamentals analysis's P/E-history context (the same adapter the pe-history endpoint
    # uses).
    return YfinanceEpsHistoryProvider()


def get_fundamentals_analysis(
    stock_info: GetStockInfo = Depends(get_stock_info),
    analyzer: FundamentalsAnalysisProvider = Depends(get_fundamentals_analysis_provider),
    # The market-routing provider supplies the daily closes for the P/E-history context (it
    # implements CandleProvider — the same instance the candle chart and pe-history endpoint
    # use, routed US→Alpaca / CA→Yahoo).
    candles: StockDataProvider = Depends(get_price_provider),
    # The industry-P/E benchmark is a pure DB read off the shared anchor (the same screened
    # universe the /stocks/industries/{industry}/pe endpoint groups on) — best-effort context
    # for the fundamentals read, so a miss just omits it.
    db: Session = Depends(get_db),
) -> GetFundamentalsAnalysis:
    # Reuses the stock snapshot wiring wholesale (price + forward estimates), then overlays the
    # trailing fundamentals (margins, valuation, dividend, market cap) from the shared anchor —
    # the DB-canonical figures the syncs materialize, replacing the retired live Finnhub call —
    # before layering the analyzer, the industry benchmark, the P/E-history read, and the
    # read-through result cache (a fresh stored read within the TTL skips the whole gather +
    # model call). The quarterly provider is the same DB-only cache the per-stock scorecard
    # uses, backing the consensus P/E. The P/E-history read (candles + the keyless EPS adapter)
    # is the one non-DB-only context leg — best-effort, so a Yahoo block just omits the "cheap
    # for this stock?" signal; the result cache amortizes its live legs to once per TTL.
    return GetFundamentalsAnalysis(
        stock_info,
        analyzer,
        SqlStockSearchRepository(db),
        DbOnlyQuarterlyEarningsProvider(SqlQuarterlyEarningsRepository(db)),
        pe_history=GetStockPeHistory(candles, _eps_history_provider()),
        cache=fundamentals_analysis_cache(db),
        cache_ttl=analysis_cache_ttl("fundamentals"),
    )


# Authored by the service, not the model: the analysis is informational only.
_ANALYSIS_DISCLAIMER = (
    "AI-generated for informational and educational purposes only — not financial "
    "advice. Markets carry risk; do your own research before investing."
)


def _present_scorecard(scorecard: StockScorecard) -> InvestmentAnalysisResponse:
    return InvestmentAnalysisResponse(
        symbol=scorecard.symbol,
        recommendation=scorecard.recommendation.value,
        confidence=scorecard.confidence.value,
        thesis=scorecard.thesis,
        sections=[
            ScorecardSectionResponse(
                key=section.key,
                title=section.title,
                stance=section.stance.value,
                label=section.label,
                summary=section.summary,
                metrics=[
                    SectionMetricResponse(label=m.label, value=m.value)
                    for m in section.metrics
                ],
            )
            for section in scorecard.sections
        ],
        disclaimer=_ANALYSIS_DISCLAIMER,
        model=scorecard.model,
        generated_at=scorecard.generated_at,
    )


def _present_earnings_analysis(
    analysis: EarningsAnalysis,
) -> EarningsAnalysisResponse:
    return EarningsAnalysisResponse(
        symbol=analysis.symbol,
        summary=analysis.summary,
        trend=analysis.trend.value,
        highlights=list(analysis.highlights),
        disclaimer=_ANALYSIS_DISCLAIMER,
        model=analysis.model,
        generated_at=analysis.generated_at,
    )


def _present_ratings_analysis(
    analysis: RatingsAnalysis,
) -> RatingsAnalysisResponse:
    return RatingsAnalysisResponse(
        symbol=analysis.symbol,
        verdict=analysis.verdict.value,
        confidence=analysis.confidence.value,
        summary=analysis.summary,
        findings=list(analysis.findings),
        disclaimer=_ANALYSIS_DISCLAIMER,
        model=analysis.model,
        generated_at=analysis.generated_at,
    )


def _present_fundamentals_analysis(
    analysis: FundamentalsAnalysis,
) -> FundamentalsAnalysisResponse:
    return FundamentalsAnalysisResponse(
        symbol=analysis.symbol,
        verdict=analysis.verdict.value,
        confidence=analysis.confidence.value,
        summary=analysis.summary,
        findings=list(analysis.findings),
        disclaimer=_ANALYSIS_DISCLAIMER,
        model=analysis.model,
        generated_at=analysis.generated_at,
    )


def _present_sector_mover(mover: SectorMover) -> SectorMoverResponse:
    return SectorMoverResponse(
        ticker=mover.ticker,
        name=mover.name,
        change_percent=mover.change_percent,
        market_cap=mover.market_cap,
    )


def _present_sector_headline(headline: SectorHeadline) -> SectorHeadlineResponse:
    return SectorHeadlineResponse(
        ticker=headline.ticker,
        title=headline.title,
        published_at=headline.published_at,
        publisher=headline.publisher,
        link=headline.link,
    )


def _present_sector_highlight(highlight: SectorHighlight) -> SectorHighlightResponse:
    return SectorHighlightResponse(
        sector=highlight.sector,
        symbol=highlight.symbol,
        change_percent=highlight.change_percent,
        note=highlight.note,
        movers=[_present_sector_mover(m) for m in highlight.movers],
        headlines=[_present_sector_headline(h) for h in highlight.headlines],
    )


def _present_sector_analysis(analysis: SectorAnalysis) -> SectorAnalysisResponse:
    return SectorAnalysisResponse(
        summary=analysis.summary,
        tone=analysis.tone.value,
        leaders=[_present_sector_highlight(h) for h in analysis.leaders],
        laggards=[_present_sector_highlight(h) for h in analysis.laggards],
        disclaimer=_ANALYSIS_DISCLAIMER,
        model=analysis.model,
        generated_at=analysis.generated_at,
    )


def _present_market_index_return(
    index_return: MarketIndexReturn,
) -> MarketIndexReturnResponse:
    return MarketIndexReturnResponse(
        name=index_return.name,
        symbol=index_return.symbol,
        change_percent=index_return.change_percent,
    )


def _present_market_period(period: MarketPeriodHighlight) -> MarketPeriodResponse:
    return MarketPeriodResponse(
        period=period.period.value,
        indexes=[_present_market_index_return(r) for r in period.indexes],
        note=period.note,
    )


def _present_market_summary(summary: MarketSummary) -> MarketSummaryResponse:
    return MarketSummaryResponse(
        summary=summary.summary,
        tone=summary.tone.value,
        periods=[_present_market_period(p) for p in summary.periods],
        disclaimer=_ANALYSIS_DISCLAIMER,
        model=summary.model,
        generated_at=summary.generated_at,
    )


@router.get("/stocks/{symbol}/analysis", response_model=InvestmentAnalysisResponse)
@limiter.limit(_AI_ANALYSIS_RATE_LIMIT)
def get_stock_analysis_endpoint(
    request: Request,
    symbol: str,
    response: Response,
    use_case: GetStockAnalysis = Depends(get_stock_analysis),
) -> InvestmentAnalysisResponse:
    try:
        analysis = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # The model call is slow and metered, and an analysis only drifts as the
    # underlying figures do — cache briefly so a burst of viewers collapses onto
    # one generation rather than re-billing per request.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present_scorecard(analysis)


@router.get(
    "/stocks/{symbol}/earnings/analysis",
    response_model=EarningsAnalysisResponse,
)
@limiter.limit(_AI_ANALYSIS_RATE_LIMIT)
def get_earnings_analysis_endpoint(
    request: Request,
    symbol: str,
    response: Response,
    use_case: GetEarningsAnalysis = Depends(get_earnings_analysis),
) -> EarningsAnalysisResponse:
    try:
        analysis = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # The model call is slow and metered, and an earnings read only drifts as the
    # reported figures do — cache briefly so a burst of viewers collapses onto one
    # generation rather than re-billing per request.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present_earnings_analysis(analysis)


@router.get(
    "/stocks/ticker/{ticker}/analyst-info/analysis",
    response_model=RatingsAnalysisResponse,
)
@limiter.limit(_AI_ANALYSIS_RATE_LIMIT)
def get_ratings_analysis_endpoint(
    request: Request,
    ticker: str,
    response: Response,
    use_case: GetRatingsFindings = Depends(get_ratings_findings),
) -> RatingsAnalysisResponse:
    try:
        analysis = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # The model call is slow and metered, and analyst coverage only drifts as ratings do —
    # cache briefly so a burst of viewers collapses onto one generation rather than re-billing.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present_ratings_analysis(analysis)


@router.get(
    "/stocks/{symbol}/fundamentals/analysis",
    response_model=FundamentalsAnalysisResponse,
)
@limiter.limit(_AI_ANALYSIS_RATE_LIMIT)
def get_fundamentals_analysis_endpoint(
    request: Request,
    symbol: str,
    response: Response,
    use_case: GetFundamentalsAnalysis = Depends(get_fundamentals_analysis),
) -> FundamentalsAnalysisResponse:
    try:
        analysis = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # The model call is slow and metered, and a fundamentals read only drifts as the reported
    # figures do — cache briefly so a burst of viewers collapses onto one generation rather
    # than re-billing per request.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present_fundamentals_analysis(analysis)


@router.get("/sectors/analysis", response_model=SectorAnalysisResponse)
@limiter.limit(_AI_ANALYSIS_RATE_LIMIT)
def get_sector_analysis_endpoint(
    request: Request,
    response: Response,
    use_case: GetSectorAnalysis = Depends(get_sector_analysis),
) -> SectorAnalysisResponse:
    try:
        analysis = use_case.execute()
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # The model call is slow and metered, and a market-wide read only drifts as the
    # sector board does — cache longer than the per-stock analysis (this backs a
    # homepage widget hit by every visitor) so a burst of viewers collapses onto one
    # generation rather than re-billing per request.
    response.headers["Cache-Control"] = "public, max-age=900"
    return _present_sector_analysis(analysis)


@router.get("/market/summary", response_model=MarketSummaryResponse)
@limiter.limit(_AI_ANALYSIS_RATE_LIMIT)
def get_market_summary_endpoint(
    request: Request,
    response: Response,
    use_case: GetMarketSummary = Depends(get_market_summary),
) -> MarketSummaryResponse:
    try:
        summary = use_case.execute()
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Same caching stance as the sector read: the model call is slow and metered,
    # and a market-wide overview only drifts as the index board does. This backs a
    # homepage widget hit by every visitor, so cache generously (15 min) — a burst
    # of viewers collapses onto one generation rather than re-billing per request.
    response.headers["Cache-Control"] = "public, max-age=900"
    return _present_market_summary(summary)
