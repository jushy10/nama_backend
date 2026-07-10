"""Controller + Presenter + dependency wiring for the stocks feature.

Each controller (e.g. `get_stock_candles_endpoint`) adapts an HTTP request into a
use-case call; its presenter (e.g. `_present_candles`) adapts the returned entity
into the HTTP DTO.

Credentials are read from the environment (like DATABASE_URL in app/db.py).
The provider is built lazily so the app still boots without Alpaca keys —
the error only surfaces when the endpoint is actually called.
"""

import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.alpaca_provider import AlpacaStockDataProvider
from app.stocks.bedrock_analysis_provider import BedrockAnalysisProvider
from app.stocks.bedrock_earnings_analysis_provider import (
    BedrockEarningsAnalysisProvider,
)
from app.stocks.bedrock_market_summary_provider import BedrockMarketSummaryProvider
from app.stocks.bedrock_ratings_analysis_provider import (
    BedrockRatingsAnalysisProvider,
)
from app.stocks.bedrock_sector_analysis_provider import BedrockSectorAnalysisProvider
from app.stocks.chart_window import ChartRange, resolve_window
from app.stocks.entities import (
    CandleSeries,
    EarningsAnalysis,
    InvestmentAnalysis,
    MarketIndexReturn,
    MarketPeriodHighlight,
    MarketSummary,
    RatingsAnalysis,
    SectorAnalysis,
    SectorHighlight,
    SectorPerformance,
    StockPerformance,
    Timeframe,
)
from app.stocks.caching_company_profile_provider import CachingCompanyProfileProvider
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.finnhub_company_profile_provider import FinnhubCompanyProfileProvider
from app.stocks.finnhub_fundamentals_provider import FinnhubFundamentalsProvider
from app.stocks.logodev_provider import LogoDevProvider
from app.stocks.indicators import (
    EmaSeries,
    SupportLevelSeries,
)
from app.stocks.adapters.annual_earnings_estimates_adapter import (
    AnnualEarningsEstimatesProvider,
)
from app.stocks.adapters.db_only_context_providers import (
    DbOnlyAnnualEarningsProvider,
    DbOnlyQuarterlyEarningsProvider,
    DbOnlyRatingChangesProvider,
    DbOnlyRecommendationsProvider,
)
from app.stocks.adapters.yfinance_options_adapter import YfinanceOptionChainProvider
from app.stocks.analysis.db_repository import SqlInvestmentAnalysisCache
from app.stocks.earnings.annual.db_repository import SqlAnnualEarningsRepository
from app.stocks.earnings.quarterly.db_repository import (
    SqlQuarterlyEarningsRepository,
)
from app.stocks.ports import (
    AllTimeHighProvider,
    AnalystEstimatesProvider,
    CompanyProfileProvider,
    EarningsAnalysisProvider,
    InvestmentAnalysisCache,
    InvestmentAnalysisProvider,
    LogoProvider,
    MarketSummaryProvider,
    RatingsAnalysisProvider,
    SectorAnalysisProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
)
from app.stocks.recommendations.db_repository import (
    SqlRatingChangesRepository,
    SqlRecommendationsRepository,
)
from app.stocks.universe.db_repository import SqlStockSearchRepository
from app.stocks.schemas import (
    CandleResponse,
    CandleSeriesResponse,
    EarningsAnalysisResponse,
    EmaLineResponse,
    EmaPointResponse,
    EmaResponse,
    InvestmentAnalysisResponse,
    MarketIndexReturnResponse,
    MarketPeriodResponse,
    MarketSummaryResponse,
    RatingsAnalysisResponse,
    SectorAnalysisResponse,
    SectorBoardResponse,
    SectorHighlightResponse,
    SectorPerformanceResponse,
    StockPerformanceResponse,
    SupportLevelResponse,
    SupportLevelsResponse,
)
from app.stocks.use_cases import (
    GetEarningsAnalysis,
    GetMarketOverview,
    GetMarketSummary,
    GetRatingsFindings,
    GetSectorAnalysis,
    GetSectorPerformance,
    GetStockAnalysis,
    GetStockCandles,
    GetStockEma,
    GetStockInfo,
    GetStockLogo,
    GetStockSupportLevels,
)

router = APIRouter(tags=["stocks"])


@lru_cache(maxsize=1)
def get_provider() -> AlpacaStockDataProvider:
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise HTTPException(
            503, "Stock data is not configured (APCA_API_KEY_ID / APCA_API_SECRET_KEY)."
        )
    return AlpacaStockDataProvider(key, secret)


@lru_cache(maxsize=1)
def get_fundamentals_provider() -> StockFundamentalsProvider | None:
    # Best-effort enrichment: without a key we simply omit market cap + dividend
    # (price + performance still serve). Free key from finnhub.io.
    key = os.environ.get("FINNHUB_API_KEY")
    return FinnhubFundamentalsProvider(key) if key else None


@lru_cache(maxsize=1)
def get_profile_provider() -> CompanyProfileProvider | None:
    # The clean display name comes from Finnhub's free profile endpoint — best-effort
    # enrichment, so without a key we simply omit the name override (the price feed's
    # legal title still serves). Wrapped in a TTL cache (a singleton, so it persists
    # across requests) to stay under Finnhub's per-minute rate limit; the name is
    # near-static.
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    if not finnhub_key:
        return None
    return CachingCompanyProfileProvider(FinnhubCompanyProfileProvider(finnhub_key))


@lru_cache(maxsize=1)
def get_options_provider() -> YfinanceOptionChainProvider:
    # The ticker card's options read comes from Yahoo via yfinance — keyless,
    # like the earnings timelines' live source, so there's no key gate here at
    # all. Best-effort enrichment: a blocked Yahoo call leaves the block null
    # rather than sinking the card, so the provider is always wired.
    return YfinanceOptionChainProvider()


def get_estimates_provider(
    db: Session = Depends(get_db),
) -> AnalystEstimatesProvider:
    # Forward analyst estimates back the ticker card's forward PEG and the AI
    # analysis context — best-effort enrichment. They're projected from the
    # annual-earnings slice's stored forward years (the same Yahoo consensus that
    # timeline serves), DB-only: a symbol whose timeline isn't cached yet just
    # omits the forward metrics until the annual read path or its cron fills the
    # rows. No second table, fetch, or cron.
    return AnnualEarningsEstimatesProvider(SqlAnnualEarningsRepository(db))


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


def get_stock_candles(
    # The Alpaca provider implements CandleProvider too, so the same instance
    # serves both the snapshot and candle endpoints.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetStockCandles:
    return GetStockCandles(provider)


def get_stock_ema(
    # EMA rides on the same CandleProvider as candles — it's derived from the
    # OHLC bars, so the Alpaca instance backs this endpoint too.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetStockEma:
    return GetStockEma(provider)


def get_stock_support_levels(
    # Support levels ride on the same CandleProvider as candles — they're
    # detected from the OHLC bars, so the Alpaca instance backs this endpoint too.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetStockSupportLevels:
    return GetStockSupportLevels(provider)


def get_sector_performance(
    # The Alpaca provider implements SectorPerformanceProvider as well, reading
    # each sector through its proxy ETF snapshot.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetSectorPerformance:
    return GetSectorPerformance(provider)


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


def analysis_cache_ttl() -> timedelta:
    # How long a stored analysis is served before it's regenerated. Config with a
    # sane default (30 min) — an analysis only drifts as its underlying figures do,
    # and every served read carries its own `generated_at` so the age is visible.
    minutes = os.environ.get("ANALYSIS_CACHE_TTL_MINUTES")
    try:
        return timedelta(minutes=float(minutes)) if minutes else timedelta(minutes=30)
    except ValueError:
        return timedelta(minutes=30)


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


def get_market_overview(
    # The Alpaca provider implements MarketOverviewProvider too, reading the S&P
    # 500 and Nasdaq through their proxy ETFs (SPY / QQQ) — same as the sectors.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetMarketOverview:
    return GetMarketOverview(provider)


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


@lru_cache(maxsize=1)
def get_logo_provider() -> LogoProvider:
    # Logo.dev keeps logos current through rebrands/symbol changes. It needs a
    # free *publishable* token (logo.dev, 500k/mo); without it the logo endpoint
    # returns 503, mirroring how the Alpaca keys gate price data. LOGODEV_BASE_URL
    # lets tests point elsewhere without a code change.
    token = os.environ.get("LOGODEV_TOKEN")
    if not token:
        raise HTTPException(503, "Logos are not configured (LOGODEV_TOKEN).")
    base_url = os.environ.get("LOGODEV_BASE_URL")
    return LogoDevProvider(token, base_url) if base_url else LogoDevProvider(token)


def get_stock_logo(provider: LogoProvider = Depends(get_logo_provider)) -> GetStockLogo:
    return GetStockLogo(provider)


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


def _present_candles(series: CandleSeries) -> CandleSeriesResponse:
    """Presenter: candle series entity -> HTTP response DTO."""
    return CandleSeriesResponse(
        symbol=series.symbol,
        timeframe=series.timeframe.value,
        count=len(series.candles),
        candles=[
            CandleResponse(
                time=int(c.timestamp.timestamp()),
                timestamp=c.timestamp,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                direction="up" if c.is_bullish else "down",
            )
            for c in series.candles
        ],
    )


def _present_ema(series: EmaSeries) -> EmaResponse:
    """Presenter: EMA series entity -> HTTP response DTO (one line per period)."""
    return EmaResponse(
        symbol=series.symbol,
        timeframe=series.timeframe.value,
        lines=[
            EmaLineResponse(
                period=line.period,
                count=len(line.points),
                latest=line.latest.value if line.latest else None,
                points=[
                    EmaPointResponse(
                        time=int(point.timestamp.timestamp()),
                        timestamp=point.timestamp,
                        value=point.value,
                    )
                    for point in line.points
                ],
            )
            for line in series.lines
        ],
    )


def _present_support_levels(series: SupportLevelSeries) -> SupportLevelsResponse:
    """Presenter: support-level series entity -> HTTP response DTO."""
    return SupportLevelsResponse(
        symbol=series.symbol,
        timeframe=series.timeframe.value,
        reference_price=series.reference_price,
        count=len(series.levels),
        levels=[
            SupportLevelResponse(
                price=level.price,
                touches=level.touches,
                last_touched=level.last_touched,
                strength=level.strength.value,
                distance_percent=level.distance_percent,
            )
            for level in series.levels
        ],
    )


def _present_sectors(sectors: list[SectorPerformance]) -> SectorBoardResponse:
    """Presenter: ranked sector entities -> HTTP response DTO."""
    return SectorBoardResponse(
        count=len(sectors),
        sectors=[
            SectorPerformanceResponse(
                sector=s.sector,
                symbol=s.symbol,
                price=s.price,
                change=s.change,
                change_percent=s.change_percent,
                previous_close=s.previous_close,
                as_of=s.as_of,
                performance=_present_performance(s.performance),
            )
            for s in sectors
        ],
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


def _as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a (possibly naive) query datetime to UTC so window arithmetic and
    comparisons never mix naive and aware values."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


@router.get(
    "/stocks/{symbol}/logo",
    responses={200: {"content": {"image/png": {}}}},
    response_class=Response,
)
def get_stock_logo_image(
    symbol: str, use_case: GetStockLogo = Depends(get_stock_logo)
) -> Response:
    try:
        logo = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return Response(content=logo.content, media_type=logo.media_type)


@router.get("/stocks/ticker/{ticker}/candles", response_model=CandleSeriesResponse)
def get_stock_candles_endpoint(
    ticker: str,
    timeframe: Timeframe = Query(
        Timeframe.DAY_1, description="Granularity of each candle."
    ),
    range_: ChartRange = Query(
        ChartRange.MONTH_6,
        alias="range",
        description="How far back to fetch. Ignored when an explicit `start`/`end` is given.",
    ),
    start: datetime | None = Query(
        None, description="Explicit window start (ISO 8601, UTC). Overrides `range`."
    ),
    end: datetime | None = Query(
        None, description="Explicit window end (ISO 8601, UTC). Defaults to now."
    ),
    use_case: GetStockCandles = Depends(get_stock_candles),
) -> CandleSeriesResponse:
    start, end = _as_utc(start), _as_utc(end)
    # Explicit start/end win; otherwise derive the window from the range preset.
    if start is None and end is None:
        start, end = resolve_window(range_, now=datetime.now(timezone.utc))
    elif end is None:
        end = datetime.now(timezone.utc)

    try:
        series = use_case.execute(ticker, timeframe, start=start, end=end)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present_candles(series)


# EMA overlay bounds: a chart draws a handful of moving-average lines, each a
# lookback of at least a couple of bars and no longer than a few hundred (the
# 200-EMA is the deepest common one; leave headroom above it).
_EMA_MIN_PERIOD = 2
_EMA_MAX_PERIOD = 400
_EMA_MAX_LINES = 5


def _normalize_ema_periods(periods: list[int]) -> list[int]:
    """Validate + de-duplicate the requested EMA periods, preserving request order.

    Rejects an out-of-range period, an empty set, or more lines than a chart
    should carry — a 400, since these are client inputs.
    """
    seen: dict[int, None] = {}
    for period in periods:
        if not _EMA_MIN_PERIOD <= period <= _EMA_MAX_PERIOD:
            raise HTTPException(
                400,
                f"EMA period must be between {_EMA_MIN_PERIOD} and {_EMA_MAX_PERIOD}.",
            )
        seen[period] = None  # dict keeps insertion order and drops duplicates
    unique = list(seen)
    if not unique:
        raise HTTPException(400, "At least one EMA period is required.")
    if len(unique) > _EMA_MAX_LINES:
        raise HTTPException(
            400, f"At most {_EMA_MAX_LINES} EMA periods can be requested at once."
        )
    return unique


@router.get("/stocks/ticker/{ticker}/ema", response_model=EmaResponse)
def get_stock_ema_endpoint(
    ticker: str,
    timeframe: Timeframe = Query(
        Timeframe.DAY_1, description="Granularity each EMA is computed over."
    ),
    range_: ChartRange = Query(
        ChartRange.MONTH_6,
        alias="range",
        description="How far back to fetch closes. Ignored when `start`/`end` is given.",
    ),
    period: list[int] = Query(
        [9, 21, 50],
        description=(
            "EMA lookback(s) in candles; repeat the param for multiple overlay "
            "lines (e.g. period=9&period=21&period=50). Defaults to 9/21/50."
        ),
    ),
    start: datetime | None = Query(
        None, description="Explicit window start (ISO 8601, UTC). Overrides `range`."
    ),
    end: datetime | None = Query(
        None, description="Explicit window end (ISO 8601, UTC). Defaults to now."
    ),
    use_case: GetStockEma = Depends(get_stock_ema),
) -> EmaResponse:
    periods = _normalize_ema_periods(period)
    start, end = _as_utc(start), _as_utc(end)
    # Explicit start/end win; otherwise derive the window from the range preset.
    if start is None and end is None:
        start, end = resolve_window(range_, now=datetime.now(timezone.utc))
    elif end is None:
        end = datetime.now(timezone.utc)

    try:
        series = use_case.execute(
            ticker, timeframe, periods=periods, start=start, end=end
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present_ema(series)


@router.get(
    "/stocks/ticker/{ticker}/support-levels", response_model=SupportLevelsResponse
)
def get_stock_support_levels_endpoint(
    ticker: str,
    timeframe: Timeframe = Query(
        Timeframe.DAY_1, description="Granularity of the candles the levels are detected from."
    ),
    range_: ChartRange = Query(
        ChartRange.YEAR_1,
        alias="range",
        description=(
            "How far back to scan for swing lows. Defaults to 1Y so levels stay "
            "meaningful independently of the chart's zoom. Ignored when an explicit "
            "`start`/`end` is given."
        ),
    ),
    window: int = Query(
        5,
        ge=2,
        le=50,
        description="Swing-low lookback in candles on each side (a pivot low is the lowest within this many bars).",
    ),
    tolerance: float = Query(
        0.02,
        gt=0.0,
        lt=1.0,
        description="Price band that merges nearby lows into one level, as a fraction (0.02 = 2%).",
    ),
    max_levels: int = Query(
        5, ge=1, le=20, description="Maximum number of levels to return."
    ),
    start: datetime | None = Query(
        None, description="Explicit window start (ISO 8601, UTC). Overrides `range`."
    ),
    end: datetime | None = Query(
        None, description="Explicit window end (ISO 8601, UTC). Defaults to now."
    ),
    use_case: GetStockSupportLevels = Depends(get_stock_support_levels),
) -> SupportLevelsResponse:
    start, end = _as_utc(start), _as_utc(end)
    # Explicit start/end win; otherwise derive the window from the range preset.
    if start is None and end is None:
        start, end = resolve_window(range_, now=datetime.now(timezone.utc))
    elif end is None:
        end = datetime.now(timezone.utc)

    try:
        series = use_case.execute(
            ticker,
            timeframe,
            window=window,
            tolerance=tolerance,
            max_levels=max_levels,
            start=start,
            end=end,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present_support_levels(series)


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


@router.get("/sectors", response_model=SectorBoardResponse)
def get_sectors_endpoint(
    use_case: GetSectorPerformance = Depends(get_sector_performance),
) -> SectorBoardResponse:
    try:
        sectors = use_case.execute()
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present_sectors(sectors)


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
