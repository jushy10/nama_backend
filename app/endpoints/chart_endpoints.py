from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.domains.pricing.charts import wiring
from app.domains.pricing.charts.api_schemas import (
    CandleSeriesResponse,
    EmaResponse,
    IndicatorsResponse,
    SupportLevelsResponse,
    TrendResponse,
)
from app.domains.pricing.charts.chart_window import ChartRange, resolve_window
from app.domains.pricing.charts.indicators import INDICATOR_NAMES, IndicatorSpec
from app.domains.pricing.charts.interfaces import CandleAdapter
from app.domains.pricing.charts.use_cases import (
    GetStockCandles,
    GetStockEma,
    GetStockIndicators,
    GetStockSupportLevels,
    GetStockTrend,
)
from app.domains.shared.entities import Timeframe
from app.endpoints.wiring import get_price_provider

router = APIRouter(tags=["charts"])


def get_stock_candles(
    # Depends shim over the slice's wiring. The market-routing provider implements
    # CandleAdapter, so one instance serves the chart for either market — a US symbol
    # reads Alpaca bars, a Canadian-suffixed one Yahoo bars.
    provider: CandleAdapter = Depends(get_price_provider),
) -> GetStockCandles:
    return wiring.build_get_stock_candles(provider)


def get_stock_ema(
    # EMA rides on the same CandleAdapter as candles — derived from the OHLC bars, so the
    # routing provider (US→Alpaca / CA→Yahoo) backs this endpoint too.
    provider: CandleAdapter = Depends(get_price_provider),
) -> GetStockEma:
    return wiring.build_get_stock_ema(provider)


def get_stock_support_levels(
    # Support levels ride on the same CandleAdapter as candles — detected from the OHLC bars,
    # so the routing provider backs this endpoint too.
    provider: CandleAdapter = Depends(get_price_provider),
) -> GetStockSupportLevels:
    return wiring.build_get_stock_support_levels(provider)


def get_stock_trend(
    # Trend rides on the same CandleAdapter as candles — read from the OHLC bars (EMA slopes),
    # so the routing provider backs this endpoint too.
    provider: CandleAdapter = Depends(get_price_provider),
) -> GetStockTrend:
    return wiring.build_get_stock_trend(provider)


def get_stock_indicators(
    # The indicator bundle rides on the same CandleAdapter as candles — every indicator is
    # derived from the OHLCV bars, so the routing provider backs it too.
    provider: CandleAdapter = Depends(get_price_provider),
) -> GetStockIndicators:
    return wiring.build_get_stock_indicators(provider)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _resolve_request_window(
    range_: ChartRange, start: datetime | None, end: datetime | None
) -> tuple[datetime | None, datetime]:
    start, end = _as_utc(start), _as_utc(end)
    if start is None and end is None:
        return resolve_window(range_, now=datetime.now(timezone.utc))
    if end is None:
        end = datetime.now(timezone.utc)
    return start, end


@contextmanager
def _translate_value_errors():
    # Bad request input (invalid symbol / inverted window / bad periods) surfaces as a
    # ValueError from the use case — an inline 400, deliberately kept in the endpoint.
    # Domain errors (StockNotFound → 404, StockDataUnavailable → 502) are translated by
    # the central handlers in endpoints/error_handlers.py.
    try:
        yield
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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
    with _translate_value_errors():
        series = use_case.run(ticker, timeframe, start=start, end=end)
    return CandleSeriesResponse.from_series(series)


# EMA overlay bounds: a chart draws a handful of moving-average lines, each a
# lookback of at least a couple of bars and no longer than a few hundred (the
# 200-EMA is the deepest common one; leave headroom above it).
_EMA_MIN_PERIOD = 2
_EMA_MAX_PERIOD = 400
_EMA_MAX_LINES = 5


def _normalize_ema_periods(periods: list[int]) -> list[int]:
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
    with _translate_value_errors():
        series = use_case.run(
            ticker, timeframe, periods=periods, start=start, end=end
        )
    return EmaResponse.from_ema(series)


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
    with _translate_value_errors():
        series = use_case.run(
            ticker,
            timeframe,
            window=window,
            tolerance=tolerance,
            max_levels=max_levels,
            start=start,
            end=end,
        )
    return SupportLevelsResponse.from_support_levels(series)


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
    price_threshold: float = Query(
        1.0,
        ge=0.0,
        le=50.0,
        description=(
            "How far (percent) the close must sit from a horizon's EMA before its "
            "position overrides the line's slope in that horizon's effective "
            "direction. Larger = price must break further from the line to count."
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
    with _translate_value_errors():
        assessment = use_case.run(
            ticker,
            timeframe,
            short_period=short_period,
            medium_period=medium_period,
            long_period=long_period,
            deadband_percent=flat_threshold,
            price_deadband_percent=price_threshold,
            start=start,
            end=end,
        )
    return TrendResponse.from_assessment(assessment)


# Indicator-request bounds: a `name:period` token's period must be a sane lookback,
# and one call carries at most a handful of indicators (a chart won't render dozens).
_INDICATOR_MIN_PERIOD = 2
_INDICATOR_MAX_PERIOD = 400
_INDICATOR_MAX_COUNT = 12


def _parse_indicator_specs(raw: str) -> list[IndicatorSpec]:
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
    with _translate_value_errors():
        result = use_case.run(ticker, timeframe, specs=specs, start=start, end=end)
    return IndicatorsResponse.from_indicator_set(result)
