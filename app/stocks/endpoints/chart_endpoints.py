"""HTTP API for the chart reads: candles, EMA overlays, support levels.

``GET /stocks/ticker/{ticker}/candles`` / ``.../ema`` / ``.../support-levels`` —
controller + presenter + wiring, the composition-root way, sitting in
``app/stocks/endpoints/`` beside the other read endpoints. All three ride the
same Alpaca ``CandleProvider`` (the shared price-feed singleton from
``wiring.py``, with its missing-keys 503 gate).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.stocks.charts.chart_window import ChartRange, resolve_window
from app.stocks.charts.indicators import (
    EmaSeries,
    HorizonTrend,
    SupportLevelSeries,
    TrendAssessment,
)
from app.stocks.charts.schemas import (
    CandleResponse,
    CandleSeriesResponse,
    EmaLineResponse,
    EmaPointResponse,
    EmaResponse,
    HorizonTrendResponse,
    SupportLevelResponse,
    SupportLevelsResponse,
    TrendResponse,
)
from app.stocks.charts.use_cases import (
    GetStockCandles,
    GetStockEma,
    GetStockSupportLevels,
    GetStockTrend,
)
from app.stocks.adapters.alpaca_adapter import AlpacaStockDataProvider
from app.stocks.entities import CandleSeries, Timeframe
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.wiring import get_provider

router = APIRouter(tags=["charts"])


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


def get_stock_trend(
    # Trend rides on the same CandleProvider as candles — it's read from the OHLC
    # bars (EMA slopes), so the Alpaca instance backs this endpoint too.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetStockTrend:
    return GetStockTrend(provider)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a (possibly naive) query datetime to UTC so window arithmetic and
    comparisons never mix naive and aware values."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


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


def _present_horizon(horizon: HorizonTrend | None) -> HorizonTrendResponse | None:
    """Presenter: one horizon's trend entity -> DTO (None passes through)."""
    if horizon is None:
        return None
    return HorizonTrendResponse(
        period=horizon.period,
        lookback=horizon.lookback,
        direction=horizon.direction.value,
        slope_percent=horizon.slope_percent,
        change_percent=horizon.change_percent,
        price_vs_ema_percent=horizon.price_vs_ema_percent,
        ema=horizon.ema,
    )


def _present_trend(assessment: TrendAssessment) -> TrendResponse:
    """Presenter: trend assessment entity -> HTTP response DTO."""
    return TrendResponse(
        symbol=assessment.symbol,
        timeframe=assessment.timeframe.value,
        reference_price=assessment.reference_price,
        reading=assessment.reading.value,
        short_term=_present_horizon(assessment.short_term),
        long_term=_present_horizon(assessment.long_term),
    )


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


# Trend horizon bounds: a lookback of at least a couple of bars, up to the deepest
# common moving average (the 200-EMA long-term read; headroom above it).
_TREND_MIN_PERIOD = 2
_TREND_MAX_PERIOD = 400


@router.get("/stocks/ticker/{ticker}/trend", response_model=TrendResponse)
def get_stock_trend_endpoint(
    ticker: str,
    timeframe: Timeframe = Query(
        Timeframe.DAY_1, description="Granularity of the candles the trend is read from."
    ),
    range_: ChartRange = Query(
        ChartRange.YEAR_1,
        alias="range",
        description=(
            "How far back to read the trend from. Defaults to 1Y so the read stays "
            "meaningful independently of the chart's zoom. Ignored when an explicit "
            "`start`/`end` is given."
        ),
    ),
    short_period: int = Query(
        20,
        ge=_TREND_MIN_PERIOD,
        le=_TREND_MAX_PERIOD,
        description="Short-horizon EMA lookback in candles (the near-term trend).",
    ),
    long_period: int = Query(
        50,
        ge=_TREND_MIN_PERIOD,
        le=_TREND_MAX_PERIOD,
        description=(
            "Long-horizon EMA lookback in candles (the primary trend). Must exceed "
            "`short_period`. Try 50 (short) / 200 (long) for the classic long-term read."
        ),
    ),
    flat_threshold: float = Query(
        0.05,
        ge=0.0,
        le=5.0,
        description=(
            "Per-bar EMA slope (percent) below which a horizon reads 'sideways' "
            "rather than a weak up/down. Larger = more tolerant of drift."
        ),
    ),
    start: datetime | None = Query(
        None, description="Explicit window start (ISO 8601, UTC). Overrides `range`."
    ),
    end: datetime | None = Query(
        None, description="Explicit window end (ISO 8601, UTC). Defaults to now."
    ),
    use_case: GetStockTrend = Depends(get_stock_trend),
) -> TrendResponse:
    start, end = _as_utc(start), _as_utc(end)
    # Explicit start/end win; otherwise derive the window from the range preset.
    if start is None and end is None:
        start, end = resolve_window(range_, now=datetime.now(timezone.utc))
    elif end is None:
        end = datetime.now(timezone.utc)

    try:
        assessment = use_case.execute(
            ticker,
            timeframe,
            short_period=short_period,
            long_period=long_period,
            deadband_percent=flat_threshold,
            start=start,
            end=end,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present_trend(assessment)
