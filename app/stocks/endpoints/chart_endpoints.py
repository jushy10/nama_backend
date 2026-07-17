"""HTTP API for the chart reads: candles, EMA overlays, support levels, trend, and
the technical-indicator bundle.

``GET /stocks/ticker/{ticker}/candles`` / ``.../ema`` / ``.../support-levels`` /
``.../trend`` / ``.../indicators`` — controller + presenter + wiring, the
composition-root way, sitting in ``app/stocks/endpoints/`` beside the other read
endpoints. All ride the same market-routing ``CandleProvider`` (the per-symbol
price provider from ``wiring.py``): a US symbol reads Alpaca bars (behind its
missing-keys 503 gate), a Canadian-suffixed one (``.TO``/``.V``/…) reads keyless
Yahoo bars.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.stocks.charts.chart_window import ChartRange, resolve_window
from app.stocks.charts.indicators import (
    EmaSeries,
    HorizonTrend,
    INDICATOR_NAMES,
    Indicator,
    IndicatorSet,
    IndicatorSpec,
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
    IndicatorLineResponse,
    IndicatorPointResponse,
    IndicatorResponse,
    IndicatorsResponse,
    SupportLevelResponse,
    SupportLevelsResponse,
    TrendResponse,
)
from app.stocks.charts.use_cases import (
    GetStockCandles,
    GetStockEma,
    GetStockIndicators,
    GetStockSupportLevels,
    GetStockTrend,
)
from app.stocks.charts.ports import CandleProvider
from app.stocks.entities import CandleSeries, Timeframe
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.wiring import get_price_provider

router = APIRouter(tags=["charts"])


def get_stock_candles(
    # The market-routing provider implements CandleProvider, so one instance serves the chart
    # for either market — a US symbol reads Alpaca bars, a Canadian-suffixed one Yahoo bars.
    provider: CandleProvider = Depends(get_price_provider),
) -> GetStockCandles:
    return GetStockCandles(provider)


def get_stock_ema(
    # EMA rides on the same CandleProvider as candles — derived from the OHLC bars, so the
    # routing provider (US→Alpaca / CA→Yahoo) backs this endpoint too.
    provider: CandleProvider = Depends(get_price_provider),
) -> GetStockEma:
    return GetStockEma(provider)


def get_stock_support_levels(
    # Support levels ride on the same CandleProvider as candles — detected from the OHLC bars,
    # so the routing provider backs this endpoint too.
    provider: CandleProvider = Depends(get_price_provider),
) -> GetStockSupportLevels:
    return GetStockSupportLevels(provider)


def get_stock_trend(
    # Trend rides on the same CandleProvider as candles — read from the OHLC bars (EMA slopes),
    # so the routing provider backs this endpoint too.
    provider: CandleProvider = Depends(get_price_provider),
) -> GetStockTrend:
    return GetStockTrend(provider)


def get_stock_indicators(
    # The indicator bundle rides on the same CandleProvider as candles — every indicator is
    # derived from the OHLCV bars, so the routing provider backs it too.
    provider: CandleProvider = Depends(get_price_provider),
) -> GetStockIndicators:
    return GetStockIndicators(provider)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a (possibly naive) query datetime to UTC so window arithmetic and
    comparisons never mix naive and aware values."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _resolve_request_window(
    range_: ChartRange, start: datetime | None, end: datetime | None
) -> tuple[datetime | None, datetime]:
    """Resolve the visible ``(start, end)`` window for a chart read: an explicit
    ``start``/``end`` wins; otherwise it's derived from the ``range`` preset. Both
    inputs are coerced to UTC first. Shared by every chart endpoint."""
    start, end = _as_utc(start), _as_utc(end)
    if start is None and end is None:
        return resolve_window(range_, now=datetime.now(timezone.utc))
    if end is None:
        end = datetime.now(timezone.utc)
    return start, end


@contextmanager
def _translate_domain_errors():
    """Map the chart use cases' domain errors onto HTTP status codes, uniformly:
    bad input → 400, unknown symbol → 404, upstream failure → 502."""
    try:
        yield
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc


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
        medium_term=_present_horizon(assessment.medium_term),
        long_term=_present_horizon(assessment.long_term),
    )


def _present_indicator(indicator: Indicator) -> IndicatorResponse:
    """Presenter: one indicator entity -> DTO (its line(s), each a time/value series)."""
    return IndicatorResponse(
        name=indicator.name,
        label=indicator.label,
        overlay=indicator.overlay,
        lines=[
            IndicatorLineResponse(
                key=line.key,
                count=len(line.points),
                latest=line.latest.value if line.latest else None,
                points=[
                    IndicatorPointResponse(
                        time=int(point.timestamp.timestamp()),
                        timestamp=point.timestamp,
                        value=point.value,
                    )
                    for point in line.points
                ],
            )
            for line in indicator.lines
        ],
    )


def _present_indicators(result: IndicatorSet) -> IndicatorsResponse:
    """Presenter: indicator set entity -> HTTP response DTO (one entry per indicator)."""
    return IndicatorsResponse(
        symbol=result.symbol,
        timeframe=result.timeframe.value,
        count=len(result.indicators),
        indicators=[_present_indicator(indicator) for indicator in result.indicators],
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
    start, end = _resolve_request_window(range_, start, end)
    with _translate_domain_errors():
        series = use_case.execute(ticker, timeframe, start=start, end=end)
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
        [9, 21, 50, 200],
        description=(
            "EMA lookback(s) in candles; repeat the param for multiple overlay "
            "lines (e.g. period=9&period=21&period=50). Defaults to 9/21/50/200 "
            "(the 200 is the long-term overlay)."
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
    start, end = _resolve_request_window(range_, start, end)
    with _translate_domain_errors():
        series = use_case.execute(
            ticker, timeframe, periods=periods, start=start, end=end
        )
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
    start, end = _resolve_request_window(range_, start, end)
    with _translate_domain_errors():
        series = use_case.execute(
            ticker,
            timeframe,
            window=window,
            tolerance=tolerance,
            max_levels=max_levels,
            start=start,
            end=end,
        )
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
    medium_period: int = Query(
        50,
        ge=_TREND_MIN_PERIOD,
        le=_TREND_MAX_PERIOD,
        description=(
            "Medium-horizon EMA lookback in candles (the intermediate trend). Must sit "
            "between `short_period` and `long_period`."
        ),
    ),
    long_period: int = Query(
        200,
        ge=_TREND_MIN_PERIOD,
        le=_TREND_MAX_PERIOD,
        description=(
            "Long-horizon EMA lookback in candles (the primary trend). Must exceed "
            "`medium_period`. 20 / 50 / 200 is the classic short/medium/long trio."
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
    start, end = _resolve_request_window(range_, start, end)
    with _translate_domain_errors():
        assessment = use_case.execute(
            ticker,
            timeframe,
            short_period=short_period,
            medium_period=medium_period,
            long_period=long_period,
            deadband_percent=flat_threshold,
            start=start,
            end=end,
        )
    return _present_trend(assessment)


# Indicator-request bounds: a `name:period` token's period must be a sane lookback,
# and one call carries at most a handful of indicators (a chart won't render dozens).
_INDICATOR_MIN_PERIOD = 2
_INDICATOR_MAX_PERIOD = 400
_INDICATOR_MAX_COUNT = 12


def _parse_indicator_specs(raw: str) -> list[IndicatorSpec]:
    """Parse the ``indicator`` query value — a comma-separated list of indicator
    names, each optionally carrying a ``:period`` override (e.g.
    ``rsi,macd,sma:200,rsi:21``) — into validated, de-duplicated specs in request
    order.

    Rejects (400) an empty list, an unknown name, a non-integer or out-of-range
    period, a period on an indicator that takes none, or more indicators than a chart
    should carry.
    """
    tokens = [tok.strip() for tok in raw.split(",")]
    tokens = [tok for tok in tokens if tok]
    if not tokens:
        raise HTTPException(400, "At least one indicator is required.")

    specs: list[IndicatorSpec] = []
    seen: set[tuple[str, int | None]] = set()
    for token in tokens:
        name, _, period_text = token.partition(":")
        name = name.strip().lower()
        if name not in INDICATOR_NAMES:
            raise HTTPException(
                400,
                f"Unknown indicator '{name}'. Supported: {', '.join(sorted(INDICATOR_NAMES))}.",
            )
        period: int | None = None
        if period_text:
            try:
                period = int(period_text)
            except ValueError:
                raise HTTPException(400, f"Invalid period in '{token}'.") from None
            if not _INDICATOR_MIN_PERIOD <= period <= _INDICATOR_MAX_PERIOD:
                raise HTTPException(
                    400,
                    f"Indicator period must be between {_INDICATOR_MIN_PERIOD} "
                    f"and {_INDICATOR_MAX_PERIOD}.",
                )
        key = (name, period)
        if key in seen:  # exact duplicate request — collapse it
            continue
        seen.add(key)
        specs.append(IndicatorSpec(name=name, period=period))

    if len(specs) > _INDICATOR_MAX_COUNT:
        raise HTTPException(
            400, f"At most {_INDICATOR_MAX_COUNT} indicators can be requested at once."
        )
    return specs


@router.get("/stocks/ticker/{ticker}/indicators", response_model=IndicatorsResponse)
def get_stock_indicators_endpoint(
    ticker: str,
    indicator: str = Query(
        ...,
        description=(
            "Comma-separated indicators to compute, e.g. `indicator=rsi,macd,bbands`. "
            "Each may carry an optional `:period` override (e.g. `rsi:21`, `sma:200`); "
            "request the same one at several periods with `sma:50,sma:200`. "
            "Supported: rsi, macd, bbands, atr, stoch, adx, obv, vwap, willr, cci, "
            "roc, mfi, sma, ema. MACD/OBV/VWAP take no period."
        ),
    ),
    timeframe: Timeframe = Query(
        Timeframe.DAY_1, description="Granularity of the candles the indicators are computed over."
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
    use_case: GetStockIndicators = Depends(get_stock_indicators),
) -> IndicatorsResponse:
    specs = _parse_indicator_specs(indicator)
    start, end = _resolve_request_window(range_, start, end)
    with _translate_domain_errors():
        result = use_case.execute(ticker, timeframe, specs=specs, start=start, end=end)
    return _present_indicators(result)
