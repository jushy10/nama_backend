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

from app.stocks.alpaca_provider import AlpacaStockDataProvider
from app.stocks.chart_window import ChartRange, resolve_window
from app.stocks.entities import (
    CandleSeries,
    KeyMetrics,
    SectorPerformance,
    Stock,
    StockPerformance,
    Timeframe,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.finnhub_fundamentals_provider import FinnhubFundamentalsProvider
from app.stocks.fmp_logo_provider import FmpLogoProvider
from app.stocks.ports import (
    LogoProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
)
from app.stocks.schemas import (
    CandleResponse,
    CandleSeriesResponse,
    KeyMetricsResponse,
    SectorBoardResponse,
    SectorPerformanceResponse,
    StockPerformanceResponse,
    StockResponse,
)
from app.stocks.use_cases import (
    GetSectorPerformance,
    GetStockCandles,
    GetStockInfo,
    GetStockLogo,
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


def get_stock_info(
    provider: StockDataProvider = Depends(get_provider),
    fundamentals: StockFundamentalsProvider | None = Depends(get_fundamentals_provider),
) -> GetStockInfo:
    # The Alpaca provider supplies both the snapshot and the performance windows.
    performance = provider if isinstance(provider, StockPerformanceProvider) else None
    return GetStockInfo(provider, performance, fundamentals)


def get_stock_candles(
    # The Alpaca provider implements CandleProvider too, so the same instance
    # serves both the snapshot and candle endpoints.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetStockCandles:
    return GetStockCandles(provider)


def get_sector_performance(
    # The Alpaca provider implements SectorPerformanceProvider as well, reading
    # each sector through its proxy ETF snapshot.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetSectorPerformance:
    return GetSectorPerformance(provider)


@lru_cache(maxsize=1)
def get_logo_provider() -> LogoProvider:
    # No credentials needed; the source is free. LOGO_BASE_URL lets you point
    # at a different ticker-keyed source without a code change.
    base_url = os.environ.get("LOGO_BASE_URL")
    return FmpLogoProvider(base_url) if base_url else FmpLogoProvider()


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
    if metrics is None:
        return None
    return KeyMetricsResponse(
        pe=metrics.pe,
        pb=metrics.pb,
        ps=metrics.ps,
        eps=metrics.eps,
        roe=metrics.roe,
        gross_margin=metrics.gross_margin,
        operating_margin=metrics.operating_margin,
        net_margin=metrics.net_margin,
        current_ratio=metrics.current_ratio,
        debt_to_equity=metrics.debt_to_equity,
        eps_growth_yoy=metrics.eps_growth_yoy,
        revenue_growth_yoy=metrics.revenue_growth_yoy,
        beta=metrics.beta,
        week_52_high=metrics.week_52_high,
        week_52_low=metrics.week_52_low,
        payout_ratio=metrics.payout_ratio,
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
