"""Controller + Presenter + dependency wiring for the stocks feature.

Each controller (e.g. `get_stock_quote_endpoint`) adapts an HTTP request into a
use-case call; its presenter (e.g. `_present_quote`) adapts the returned entity
into the HTTP DTO.

Credentials are read from the environment (like DATABASE_URL in app/db.py).
The provider is built lazily so the app still boots without Alpaca keys —
the error only surfaces when the endpoint is actually called.
"""

import os
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.alpaca_provider import AlpacaStockDataProvider
from app.stocks.bedrock_analysis_provider import BedrockAnalysisProvider
from app.stocks.chart_window import ChartRange, resolve_window
from app.stocks.entities import (
    CandleSeries,
    InvestmentAnalysis,
    Quote,
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
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RsiSeries,
    SupportLevelSeries,
)
from app.stocks.adapters.annual_earnings_estimates_adapter import (
    AnnualEarningsEstimatesProvider,
)
from app.stocks.adapters.yfinance_options_adapter import YfinanceOptionChainProvider
from app.stocks.earnings.annual.db_repository import SqlAnnualEarningsRepository
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.endpoints.quarterly_earnings_endpoints import (
    get_quarterly_earnings_provider,
)
from app.stocks.ports import (
    AllTimeHighProvider,
    AnalystEstimatesProvider,
    CompanyProfileProvider,
    InvestmentAnalysisProvider,
    LogoProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
)
from app.stocks.schemas import (
    CandleResponse,
    CandleSeriesResponse,
    InvestmentAnalysisResponse,
    QuoteResponse,
    RsiPointResponse,
    RsiResponse,
    SectorBoardResponse,
    SectorPerformanceResponse,
    StockPerformanceResponse,
    SupportLevelResponse,
    SupportLevelsResponse,
)
from app.stocks.use_cases import (
    GetSectorPerformance,
    GetStockAnalysis,
    GetStockCandles,
    GetStockInfo,
    GetStockLogo,
    GetStockQuote,
    GetStockRsi,
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


def get_stock_quote(
    # Same Alpaca instance — the live-price poll endpoint reuses the provider
    # that backs every other price view, with no extra wiring.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetStockQuote:
    return GetStockQuote(provider)


def get_stock_candles(
    # The Alpaca provider implements CandleProvider too, so the same instance
    # serves both the snapshot and candle endpoints.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetStockCandles:
    return GetStockCandles(provider)


def get_stock_rsi(
    # RSI rides on the same CandleProvider: it's derived from the OHLC bars,
    # so the Alpaca instance backs this endpoint too.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetStockRsi:
    return GetStockRsi(provider)


def get_stock_support_levels(
    # Support levels ride on the same CandleProvider as candles and RSI — they're
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


def get_stock_analysis(
    stock_info: GetStockInfo = Depends(get_stock_info),
    analyzer: InvestmentAnalysisProvider = Depends(get_analysis_provider),
    # The quarterly timeline is best-effort *context* for the analysis — the same
    # DB-cached provider the quarterly endpoint reads through, so it costs no
    # extra vendor call for a cached symbol and needs no API key.
    earnings: QuarterlyEarningsProvider = Depends(get_quarterly_earnings_provider),
) -> GetStockAnalysis:
    # Reuses the stock snapshot wiring wholesale (price + enrichment), then layers
    # the analyzer and the best-effort earnings context on top.
    return GetStockAnalysis(stock_info, analyzer, earnings)


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


def _present_quote(quote: Quote) -> QuoteResponse:
    """Presenter: quote entity -> HTTP response DTO."""
    return QuoteResponse(
        symbol=quote.symbol,
        price=quote.price,
        change=quote.change,
        change_percent=quote.change_percent,
        previous_close=quote.previous_close,
        bid=quote.bid,
        ask=quote.ask,
        spread=quote.spread,
        as_of=quote.as_of,
    )


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


def _present_rsi(series: RsiSeries) -> RsiResponse:
    """Presenter: RSI series entity -> HTTP response DTO."""
    latest = series.latest
    signal = series.signal
    return RsiResponse(
        symbol=series.symbol,
        timeframe=series.timeframe.value,
        period=series.period,
        count=len(series.points),
        latest=latest.value if latest else None,
        signal=signal.value if signal else None,
        overbought=RSI_OVERBOUGHT,
        oversold=RSI_OVERSOLD,
        points=[
            RsiPointResponse(
                time=int(point.timestamp.timestamp()),
                timestamp=point.timestamp,
                value=point.value,
            )
            for point in series.points
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


def _as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a (possibly naive) query datetime to UTC so window arithmetic and
    comparisons never mix naive and aware values."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


@router.get("/stocks/{symbol}/quote", response_model=QuoteResponse)
def get_stock_quote_endpoint(
    symbol: str,
    response: Response,
    use_case: GetStockQuote = Depends(get_stock_quote),
) -> QuoteResponse:
    try:
        quote = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Short cache so a burst of pollers (and any CDN in front) collapses onto one
    # upstream snapshot; 2s keeps it live-ish without hitting Alpaca every refresh.
    response.headers["Cache-Control"] = "public, max-age=2"
    return _present_quote(quote)


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


@router.get("/stocks/{symbol}/candles", response_model=CandleSeriesResponse)
def get_stock_candles_endpoint(
    symbol: str,
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
        series = use_case.execute(symbol, timeframe, start=start, end=end)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present_candles(series)


@router.get("/stocks/{symbol}/rsi", response_model=RsiResponse)
def get_stock_rsi_endpoint(
    symbol: str,
    timeframe: Timeframe = Query(
        Timeframe.DAY_1, description="Granularity each RSI value is computed over."
    ),
    range_: ChartRange = Query(
        ChartRange.MONTH_6,
        alias="range",
        description="How far back to fetch closes. Ignored when `start`/`end` is given.",
    ),
    period: int = Query(
        14, ge=2, le=100, description="RSI lookback in candles (Wilder default 14)."
    ),
    start: datetime | None = Query(
        None, description="Explicit window start (ISO 8601, UTC). Overrides `range`."
    ),
    end: datetime | None = Query(
        None, description="Explicit window end (ISO 8601, UTC). Defaults to now."
    ),
    use_case: GetStockRsi = Depends(get_stock_rsi),
) -> RsiResponse:
    start, end = _as_utc(start), _as_utc(end)
    # Explicit start/end win; otherwise derive the window from the range preset.
    if start is None and end is None:
        start, end = resolve_window(range_, now=datetime.now(timezone.utc))
    elif end is None:
        end = datetime.now(timezone.utc)

    try:
        series = use_case.execute(symbol, timeframe, period=period, start=start, end=end)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present_rsi(series)


@router.get(
    "/stocks/{symbol}/support-levels", response_model=SupportLevelsResponse
)
def get_stock_support_levels_endpoint(
    symbol: str,
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
            symbol,
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
