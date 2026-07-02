"""Controller + Presenter + dependency wiring for the stocks feature.

The controller (`get_stock`) adapts an HTTP request into a use-case call; the
presenter (`_present`) adapts the returned Stock entity into the HTTP DTO.

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
from app.stocks.constituents import SqlConstituentRepository
from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    CandleSeries,
    EarningsHistory,
    EarningsMetrics,
    GrowthMetrics,
    InvestmentAnalysis,
    KeyMetrics,
    MoversBoard,
    NextEarnings,
    Quote,
    ScreenedStock,
    SectorPerformance,
    Stock,
    StockIndex,
    StockPerformance,
    Timeframe,
)
from app.stocks.caching_company_profile_provider import CachingCompanyProfileProvider
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.finnhub_company_profile_provider import FinnhubCompanyProfileProvider
from app.stocks.finnhub_earnings_calendar_provider import (
    FinnhubEarningsCalendarProvider,
)
from app.stocks.finnhub_earnings_provider import FinnhubEarningsProvider
from app.stocks.finnhub_fundamentals_provider import FinnhubFundamentalsProvider
from app.stocks.logodev_provider import LogoDevProvider
from app.stocks.indicators import RSI_OVERBOUGHT, RSI_OVERSOLD, RsiSeries
from app.stocks.adapters.annual_earnings_estimates_adapter import (
    AnnualEarningsEstimatesProvider,
)
from app.stocks.adapters.quarterly_earnings_revenue_adapter import (
    QuarterlyEarningsRevenueProvider,
)
from app.stocks.earnings.annual.db_repository import SqlAnnualEarningsRepository
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.endpoints.quarterly_earnings_endpoints import (
    get_quarterly_earnings_provider,
)
from app.stocks.ports import (
    AllTimeHighProvider,
    AnalystEstimatesProvider,
    CompanyProfileProvider,
    EarningsCalendarProvider,
    EarningsHistoryProvider,
    InvestmentAnalysisProvider,
    LogoProvider,
    RevenueHistoryProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
)
from app.stocks.schemas import (
    AllTimeHighResponse,
    AnalystEstimatesResponse,
    CandleResponse,
    CandleSeriesResponse,
    EarningsHistoryResponse,
    EarningsMetricsResponse,
    EarningsSurpriseResponse,
    GrowthMetricsResponse,
    InvestmentAnalysisResponse,
    KeyMetricsResponse,
    MoversResponse,
    NextEarningsResponse,
    QuoteResponse,
    RsiPointResponse,
    RsiResponse,
    ScreenedStockResponse,
    SectorBoardResponse,
    SectorPerformanceResponse,
    StockPerformanceResponse,
    StockResponse,
)
from app.stocks.use_cases import (
    GetSectorPerformance,
    GetStockAnalysis,
    GetStockCandles,
    GetStockEarnings,
    GetStockInfo,
    GetStockLogo,
    GetStockQuote,
    GetStockRsi,
    ScreenStocks,
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


def get_estimates_provider(
    db: Session = Depends(get_db),
) -> AnalystEstimatesProvider:
    # Forward analyst estimates back the snapshot's forward P/E — best-effort
    # enrichment. They're projected from the annual-earnings slice's stored forward
    # years (the same Yahoo consensus that timeline serves), DB-only: a symbol whose
    # timeline isn't cached yet just omits the forward metrics until the annual
    # read path or its cron fills the rows. No second table, fetch, or cron.
    return AnnualEarningsEstimatesProvider(SqlAnnualEarningsRepository(db))


def get_stock_info(
    provider: StockDataProvider = Depends(get_provider),
    fundamentals: StockFundamentalsProvider | None = Depends(get_fundamentals_provider),
    profile: CompanyProfileProvider | None = Depends(get_profile_provider),
    estimates: AnalystEstimatesProvider | None = Depends(get_estimates_provider),
) -> GetStockInfo:
    # The Alpaca provider supplies the snapshot, the performance windows, and the
    # all-time high — all derived from the same price feed, so one instance backs
    # each capability via its respective port.
    performance = provider if isinstance(provider, StockPerformanceProvider) else None
    all_time_high = provider if isinstance(provider, AllTimeHighProvider) else None
    return GetStockInfo(
        provider, performance, fundamentals, profile, all_time_high, estimates
    )


def get_stock_quote(
    # Same Alpaca instance — get_quote is just the snapshot half of get_stock,
    # so the live-price poll endpoint reuses the provider with no extra wiring.
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


def get_sector_performance(
    # The Alpaca provider implements SectorPerformanceProvider as well, reading
    # each sector through its proxy ETF snapshot.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetSectorPerformance:
    return GetSectorPerformance(provider)


@lru_cache(maxsize=1)
def get_earnings_provider() -> EarningsHistoryProvider:
    # Earnings beat history is this endpoint's primary data (not best-effort
    # enrichment like market cap), so a missing key is a hard 503 — same shape
    # as the price provider — rather than a silently empty response.
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        raise HTTPException(503, "Earnings data is not configured (FINNHUB_API_KEY).")
    return FinnhubEarningsProvider(key)


@lru_cache(maxsize=1)
def get_earnings_calendar_provider() -> EarningsCalendarProvider | None:
    # The next-report forecast is best-effort enrichment on the earnings
    # endpoint (reuses the same Finnhub key the beat history needs); omitted
    # when unconfigured rather than failing the response.
    key = os.environ.get("FINNHUB_API_KEY")
    return FinnhubEarningsCalendarProvider(key) if key else None


def get_revenue_provider(
    quarterly: QuarterlyEarningsProvider = Depends(get_quarterly_earnings_provider),
) -> RevenueHistoryProvider:
    # Reported quarterly revenue for the earnings endpoint now rides on the
    # quarterly-earnings slice's stored rows (Yahoo via the persistent DB cache,
    # kept current by the merge-preserving cron) instead of a second vendor — one
    # source of truth. A symbol never cached fills lazily on first view exactly
    # like the quarterly endpoint itself; best-effort, so a miss just omits the
    # revenue overlay.
    return QuarterlyEarningsRevenueProvider(quarterly)


def get_stock_earnings(
    provider: EarningsHistoryProvider = Depends(get_earnings_provider),
    # Trailing metrics, the next-report forecast, and per-quarter revenue are
    # best-effort enrichment on top of the beat history — a miss leaves the
    # (primary) response intact rather than failing it.
    fundamentals: StockFundamentalsProvider | None = Depends(get_fundamentals_provider),
    calendar: EarningsCalendarProvider | None = Depends(get_earnings_calendar_provider),
    revenue: RevenueHistoryProvider = Depends(get_revenue_provider),
) -> GetStockEarnings:
    return GetStockEarnings(provider, fundamentals, calendar, revenue)


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


@lru_cache(maxsize=1)
def get_analysis_earnings_provider() -> EarningsHistoryProvider | None:
    # The beat history is best-effort *context* for the analysis, not its primary
    # data — so unlike the earnings endpoint a missing Finnhub key just omits it
    # rather than 503-ing. Reuses the same key when present.
    key = os.environ.get("FINNHUB_API_KEY")
    return FinnhubEarningsProvider(key) if key else None


def get_stock_analysis(
    stock_info: GetStockInfo = Depends(get_stock_info),
    analyzer: InvestmentAnalysisProvider = Depends(get_analysis_provider),
    earnings: EarningsHistoryProvider | None = Depends(get_analysis_earnings_provider),
) -> GetStockAnalysis:
    # Reuses the stock snapshot wiring wholesale (price + enrichment), then layers
    # the analyzer and the best-effort beat history on top.
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


def _present(stock: Stock) -> StockResponse:
    """Presenter: domain entity -> HTTP response DTO."""
    return StockResponse(
        symbol=stock.symbol,
        name=stock.name,
        exchange=stock.exchange,
        price=stock.price,
        change=stock.change,
        change_percent=stock.change_percent,
        open=stock.open,
        high=stock.high,
        low=stock.low,
        previous_close=stock.previous_close,
        volume=stock.volume,
        bid=stock.bid,
        ask=stock.ask,
        spread=stock.spread,
        as_of=stock.as_of,
        market_cap=stock.market_cap,
        dividend_per_share=stock.dividend_per_share,
        dividend_yield=stock.dividend_yield,
        performance=_present_performance(stock.performance),
        metrics=_present_metrics(stock.metrics),
        analyst_estimates=_present_estimates(stock.analyst_estimates),
        forward_pe=stock.forward_pe,
        forward_ps=stock.forward_ps,
        growth=_present_growth(stock.growth),
        all_time_high=_present_all_time_high(stock.all_time_high),
        drawdown_from_high=stock.drawdown_from_high,
    )


def _present_all_time_high(high: AllTimeHigh | None) -> AllTimeHighResponse | None:
    if high is None:
        return None
    return AllTimeHighResponse(
        price=high.price, reached_on=high.reached_on, since=high.since
    )


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


def _present_metrics(metrics: KeyMetrics | None) -> KeyMetricsResponse | None:
    # Valuation + health + profitability + market; the remaining earnings-flavored
    # metrics (EPS, growth) are surfaced on the earnings endpoint.
    if metrics is None:
        return None
    return KeyMetricsResponse(
        pe=metrics.pe,
        peg=metrics.peg,
        pb=metrics.pb,
        ps=metrics.ps,
        fcf_per_share=metrics.fcf_per_share,
        roe=metrics.roe,
        gross_margin=metrics.gross_margin,
        operating_margin=metrics.operating_margin,
        net_margin=metrics.net_margin,
        current_ratio=metrics.current_ratio,
        debt_to_equity=metrics.debt_to_equity,
        beta=metrics.beta,
        week_52_high=metrics.week_52_high,
        week_52_low=metrics.week_52_low,
    )


def _present_estimates(
    estimates: AnalystEstimates | None,
) -> AnalystEstimatesResponse | None:
    # Forward consensus (FY1/FY2) on the stock snapshot; the derived forward_pe /
    # forward_ps are presented as their own top-level fields (they need the price).
    if estimates is None:
        return None
    return AnalystEstimatesResponse(
        fiscal_year=estimates.fiscal_year,
        period_end=estimates.period_end,
        eps_avg=estimates.eps_avg,
        revenue_avg=estimates.revenue_avg,
        eps_avg_fy2=estimates.eps_avg_fy2,
        fiscal_year_fy2=estimates.fiscal_year_fy2,
    )


def _present_growth(growth: GrowthMetrics | None) -> GrowthMetricsResponse | None:
    # Trailing YoY (from the Finnhub metrics) + forward 1-yr growth (FY1→FY2, from
    # the analyst estimates) — both already on the stock, combined into one block.
    if growth is None:
        return None
    return GrowthMetricsResponse(
        revenue_yoy=growth.revenue_yoy,
        eps_yoy=growth.eps_yoy,
        forward_revenue_growth=growth.forward_revenue_growth,
        forward_eps_growth=growth.forward_eps_growth,
    )


def _present_earnings_metrics(
    metrics: EarningsMetrics | None,
) -> EarningsMetricsResponse | None:
    if metrics is None:
        return None
    return EarningsMetricsResponse(
        eps=metrics.eps,
        eps_growth_yoy=metrics.eps_growth_yoy,
        revenue_growth_yoy=metrics.revenue_growth_yoy,
        gross_margin=metrics.gross_margin,
        operating_margin=metrics.operating_margin,
        net_margin=metrics.net_margin,
    )


def _present_next_earnings(
    next_report: NextEarnings | None,
) -> NextEarningsResponse | None:
    if next_report is None:
        return None
    return NextEarningsResponse(
        report_date=next_report.report_date,
        fiscal_year=next_report.fiscal_year,
        fiscal_quarter=next_report.fiscal_quarter,
        eps_estimate=next_report.eps_estimate,
        revenue_estimate=next_report.revenue_estimate,
        session=next_report.session,
    )


def _present_earnings(history: EarningsHistory) -> EarningsHistoryResponse:
    """Presenter: earnings-history entity -> HTTP response DTO."""
    return EarningsHistoryResponse(
        symbol=history.symbol,
        count=len(history.quarters),
        beats=history.beats,
        scored=history.scored,
        beat_rate=history.beat_rate,
        quarters=[
            EarningsSurpriseResponse(
                period=q.period,
                fiscal_year=q.fiscal_year,
                fiscal_quarter=q.fiscal_quarter,
                actual=q.actual,
                estimate=q.estimate,
                surprise=q.surprise,
                surprise_percent=q.surprise_percent,
                beat=q.beat,
                revenue_actual=q.revenue_actual,
            )
            for q in history.quarters
        ],
        metrics=_present_earnings_metrics(history.metrics),
        # The valuation block is the same KeyMetrics slice the stock endpoint
        # serves, so it reuses that presenter.
        valuation=_present_metrics(history.valuation),
        next_report=_present_next_earnings(history.next_report),
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


def get_screener(
    provider: AlpacaStockDataProvider = Depends(get_provider),
    db: Session = Depends(get_db),
) -> ScreenStocks:
    # Universe comes from the index_constituents table (populated by
    # scripts/sync_constituents.py); the Alpaca provider supplies the day move
    # via batched snapshots. The repository is request-scoped, like the session.
    return ScreenStocks(SqlConstituentRepository(db), provider)


def _present_screened(stock: ScreenedStock) -> ScreenedStockResponse:
    """Presenter: one screened-stock entity -> HTTP response DTO."""
    return ScreenedStockResponse(
        symbol=stock.symbol,
        name=stock.name,
        sector=stock.sector,
        price=stock.quote.price,
        change=stock.quote.change,
        change_percent=stock.quote.change_percent,
        previous_close=stock.quote.previous_close,
        as_of=stock.quote.as_of,
    )


def _present_movers(board: MoversBoard) -> MoversResponse:
    """Presenter: movers board entity -> HTTP response DTO."""
    return MoversResponse(
        index=board.index.value if board.index else None,
        sector=board.sector,
        limit=board.limit,
        universe_count=board.universe_count,
        quoted_count=board.quoted_count,
        as_of=board.as_of,
        gainers=[_present_screened(s) for s in board.gainers],
        losers=[_present_screened(s) for s in board.losers],
    )


# Declared before "/stocks/{symbol}" so this literal path wins the match —
# otherwise the dynamic route would capture "screener" as a symbol.
@router.get("/stocks/screener", response_model=MoversResponse)
def get_screener_endpoint(
    response: Response,
    index: StockIndex | None = Query(
        None, description="Limit the universe to an index. Omit for all known names."
    ),
    sector: str | None = Query(
        None,
        description=(
            "Limit to one GICS sector, e.g. 'Information Technology', "
            "'Health Care', 'Financials' (case-insensitive). Omit for all sectors."
        ),
    ),
    limit: int = Query(
        10, ge=1, le=50, description="How many names per side (gainers and losers)."
    ),
    use_case: ScreenStocks = Depends(get_screener),
) -> MoversResponse:
    try:
        board = use_case.execute(index=index, sector=sector, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Heavier than a single quote (a whole index of snapshots) and the board
    # only shifts as the market moves — cache briefly so a burst of viewers
    # collapses onto one upstream sweep.
    response.headers["Cache-Control"] = "public, max-age=15"
    return _present_movers(board)


@router.get("/stocks/{symbol}", response_model=StockResponse)
def get_stock(
    symbol: str, use_case: GetStockInfo = Depends(get_stock_info)
) -> StockResponse:
    try:
        stock = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present(stock)


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


@router.get("/stocks/{symbol}/earnings", response_model=EarningsHistoryResponse)
def get_stock_earnings_endpoint(
    symbol: str,
    use_case: GetStockEarnings = Depends(get_stock_earnings),
) -> EarningsHistoryResponse:
    try:
        history = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present_earnings(history)


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
