"""HTTP API for the AI-analysis reads.

Every Claude-on-Bedrock endpoint: the per-stock buy/hold/sell analysis, the
earnings story, the analyst-coverage review, the sector rotation read, and the
market summary. Controller + presenter + wiring, the composition-root way,
sitting in ``app/stocks/endpoints/`` beside the other read endpoints.

Wiring: each analyser is a Bedrock adapter singleton (no secret to gate on —
Bedrock authenticates through the process's AWS credentials, so the IAM policy
is what enables it; a missing 'bedrock' extra is a clean 503). Best-effort
*context* is read **DB-only** (via the slices' repositories, not their
read-through providers) so a cache miss never triggers a synchronous,
rate-limited Yahoo fetch mid-request — keeping the caches current is the
crons' job.
"""

import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.bedrock.analysis_adapter import BedrockAnalysisProvider
from app.stocks.adapters.bedrock.earnings_analysis_adapter import (
    BedrockEarningsAnalysisProvider,
)
from app.stocks.adapters.bedrock.market_summary_adapter import (
    BedrockMarketSummaryProvider,
)
from app.stocks.adapters.bedrock.ratings_analysis_adapter import (
    BedrockRatingsAnalysisProvider,
)
from app.stocks.adapters.bedrock.sector_analysis_adapter import (
    BedrockSectorAnalysisProvider,
)
from app.stocks.adapters.db_only_context_providers import (
    DbOnlyAnnualEarningsProvider,
    DbOnlyQuarterlyEarningsProvider,
    DbOnlyRatingChangesProvider,
    DbOnlyRecommendationsProvider,
)
from app.stocks.analysis.db_repository import SqlInvestmentAnalysisCache
from app.stocks.analysis.entities import (
    EarningsAnalysis,
    InvestmentAnalysis,
    MarketSummary,
    RatingsAnalysis,
    SectorAnalysis,
    SectorHighlight,
    MarketIndexReturn,
    MarketPeriodHighlight,
)
from app.stocks.analysis.ports import (
    EarningsAnalysisProvider,
    InvestmentAnalysisCache,
    InvestmentAnalysisProvider,
    MarketSummaryProvider,
    RatingsAnalysisProvider,
    SectorAnalysisProvider,
)
from app.stocks.analysis.schemas import (
    EarningsAnalysisResponse,
    InvestmentAnalysisResponse,
    MarketIndexReturnResponse,
    MarketPeriodResponse,
    MarketSummaryResponse,
    RatingsAnalysisResponse,
    SectorAnalysisResponse,
    SectorHighlightResponse,
)
from app.stocks.analysis.use_cases import (
    GetEarningsAnalysis,
    GetMarketSummary,
    GetRatingsFindings,
    GetSectorAnalysis,
    GetStockAnalysis,
    GetStockInfo,
)
from app.stocks.earnings.annual.db_repository import SqlAnnualEarningsRepository
from app.stocks.earnings.quarterly.db_repository import (
    SqlQuarterlyEarningsRepository,
)
from app.stocks.endpoints.market_endpoints import (
    get_market_overview,
    get_sector_performance,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.market.use_cases import GetMarketOverview, GetSectorPerformance
from app.stocks.ports import (
    AllTimeHighProvider,
    AnalystEstimatesProvider,
    CompanyProfileProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
)
from app.stocks.recommendations.db_repository import (
    SqlRatingChangesRepository,
    SqlRecommendationsRepository,
)
from app.stocks.universe.db_repository import SqlStockSearchRepository
from app.stocks.wiring import (
    analysis_cache_ttl,
    get_estimates_provider,
    get_fundamentals_provider,
    get_profile_provider,
    get_provider,
)

router = APIRouter(tags=["analysis"])


def get_stock_info(
    provider: StockDataProvider = Depends(get_provider),
    fundamentals: StockFundamentalsProvider | None = Depends(get_fundamentals_provider),
    profile: CompanyProfileProvider | None = Depends(get_profile_provider),
    estimates: AnalystEstimatesProvider | None = Depends(get_estimates_provider),
) -> GetStockInfo:
    # The enriched snapshot use case now serves only as the AI analysis context
    # (the standalone GET /stocks/{symbol} endpoint was removed). The Alpaca
    # provider supplies the snapshot, the performance windows, and the all-time
    # high — all derived from the same price feed, so one instance backs each
    # capability via its respective port.
    performance = provider if isinstance(provider, StockPerformanceProvider) else None
    all_time_high = provider if isinstance(provider, AllTimeHighProvider) else None
    return GetStockInfo(
        provider, performance, fundamentals, profile, all_time_high, estimates
    )


@lru_cache(maxsize=1)
def get_analysis_provider() -> InvestmentAnalysisProvider:
    # AI analysis is this endpoint's primary data, so it's required — but unlike
    # the API-key vendors there's no secret to gate on: Bedrock authenticates
    # through the process's AWS credentials (the ECS task role in production), so
    # the IAM policy is what enables it. Region + model id are config with sane
    # defaults (the model id may be a cross-region inference profile). A missing
    # 'anthropic' Bedrock extra surfaces as a clean 503 here rather than a 500.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_ANALYSIS_MODEL_ID")
    try:
        if model_id:
            return BedrockAnalysisProvider(model_id=model_id, region=region)
        return BedrockAnalysisProvider(region=region)
    except ImportError as exc:
        raise HTTPException(
            503, "AI analysis is not configured (install the 'bedrock' extra)."
        ) from exc


def get_analysis_cache(
    db: Session = Depends(get_db),
) -> InvestmentAnalysisCache:
    # The read-through result cache for the stock analysis (kind="stock", so it
    # never collides with a fund of the same ticker). One row per symbol, refreshed
    # whenever a served read ages past the use case's TTL — best-effort, so a DB
    # problem degrades to a regeneration, never an error.
    return SqlInvestmentAnalysisCache(db, "stock")


def get_stock_analysis(
    stock_info: GetStockInfo = Depends(get_stock_info),
    analyzer: InvestmentAnalysisProvider = Depends(get_analysis_provider),
    cache: InvestmentAnalysisCache = Depends(get_analysis_cache),
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
        cache_ttl=analysis_cache_ttl(),
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
    try:
        if model_id:
            return BedrockSectorAnalysisProvider(model_id=model_id, region=region)
        return BedrockSectorAnalysisProvider(region=region)
    except ImportError as exc:
        raise HTTPException(
            503, "AI analysis is not configured (install the 'bedrock' extra)."
        ) from exc


def get_sector_analysis(
    # Reuses the sector-board wiring wholesale (the Alpaca-backed
    # GetSectorPerformance), then hands the ranked board to the analyzer.
    sectors: GetSectorPerformance = Depends(get_sector_performance),
    analyzer: SectorAnalysisProvider = Depends(get_sector_analysis_provider),
) -> GetSectorAnalysis:
    return GetSectorAnalysis(sectors, analyzer)


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
    try:
        if model_id:
            return BedrockMarketSummaryProvider(model_id=model_id, region=region)
        return BedrockMarketSummaryProvider(region=region)
    except ImportError as exc:
        raise HTTPException(
            503, "AI analysis is not configured (install the 'bedrock' extra)."
        ) from exc


def get_market_summary(
    # Reuses the index-board wiring wholesale (the Alpaca-backed
    # GetMarketOverview), then hands the board to the analyzer.
    overview: GetMarketOverview = Depends(get_market_overview),
    analyzer: MarketSummaryProvider = Depends(get_market_summary_provider),
) -> GetMarketSummary:
    return GetMarketSummary(overview, analyzer)


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
    try:
        if model_id:
            return BedrockEarningsAnalysisProvider(model_id=model_id, region=region)
        return BedrockEarningsAnalysisProvider(region=region)
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
    )


# Authored by the service, not the model: the analysis is informational only.
_ANALYSIS_DISCLAIMER = (
    "AI-generated for informational and educational purposes only — not financial "
    "advice. Markets carry risk; do your own research before investing."
)


def _present_analysis(analysis: InvestmentAnalysis) -> InvestmentAnalysisResponse:
    """Presenter: investment-analysis entity -> HTTP response DTO.

    The disclaimer is attached here, at the edge — it's a property of the service,
    not something the model is trusted to author."""
    return InvestmentAnalysisResponse(
        symbol=analysis.symbol,
        recommendation=analysis.recommendation.value,
        confidence=analysis.confidence.value,
        thesis=analysis.thesis,
        strengths=list(analysis.strengths),
        risks=list(analysis.risks),
        disclaimer=_ANALYSIS_DISCLAIMER,
        model=analysis.model,
        generated_at=analysis.generated_at,
    )


def _present_earnings_analysis(
    analysis: EarningsAnalysis,
) -> EarningsAnalysisResponse:
    """Presenter: earnings-analysis entity -> HTTP response DTO.

    The disclaimer is attached here, at the edge — it's a property of the service,
    not something the model is trusted to author."""
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
    """Presenter: ratings-analysis entity -> HTTP response DTO.

    The disclaimer is attached here, at the edge — it's a property of the service,
    not something the model is trusted to author."""
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


def _present_sector_highlight(highlight: SectorHighlight) -> SectorHighlightResponse:
    """Presenter: one sector highlight entity -> HTTP response DTO."""
    return SectorHighlightResponse(
        sector=highlight.sector,
        symbol=highlight.symbol,
        change_percent=highlight.change_percent,
        note=highlight.note,
    )


def _present_sector_analysis(analysis: SectorAnalysis) -> SectorAnalysisResponse:
    """Presenter: sector-analysis entity -> HTTP response DTO.

    Same shape as ``_present_analysis`` — the disclaimer is attached here, at the
    edge, since it's a property of the service, not something the model authors."""
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
    """Presenter: one index's per-period return entity -> HTTP response DTO."""
    return MarketIndexReturnResponse(
        name=index_return.name,
        symbol=index_return.symbol,
        change_percent=index_return.change_percent,
    )


def _present_market_period(period: MarketPeriodHighlight) -> MarketPeriodResponse:
    """Presenter: one market-summary period entity -> HTTP response DTO."""
    return MarketPeriodResponse(
        period=period.period.value,
        indexes=[_present_market_index_return(r) for r in period.indexes],
        note=period.note,
    )


def _present_market_summary(summary: MarketSummary) -> MarketSummaryResponse:
    """Presenter: market-summary entity -> HTTP response DTO.

    Same shape as ``_present_sector_analysis`` — the disclaimer is attached here,
    at the edge, since it's a property of the service, not something the model
    authors."""
    return MarketSummaryResponse(
        summary=summary.summary,
        tone=summary.tone.value,
        periods=[_present_market_period(p) for p in summary.periods],
        disclaimer=_ANALYSIS_DISCLAIMER,
        model=summary.model,
        generated_at=summary.generated_at,
    )


@router.get("/stocks/{symbol}/analysis", response_model=InvestmentAnalysisResponse)
def get_stock_analysis_endpoint(
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
    return _present_analysis(analysis)


@router.get(
    "/stocks/{symbol}/earnings/analysis",
    response_model=EarningsAnalysisResponse,
)
def get_earnings_analysis_endpoint(
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
def get_ratings_analysis_endpoint(
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


@router.get("/sectors/analysis", response_model=SectorAnalysisResponse)
def get_sector_analysis_endpoint(
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
def get_market_summary_endpoint(
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
