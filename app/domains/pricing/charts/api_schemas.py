from datetime import date, datetime

from pydantic import BaseModel

from app.domains.pricing.charts.indicators import (
    EmaLine,
    EmaSeries,
    HorizonTrend,
    Indicator,
    IndicatorLine,
    IndicatorSet,
    SupportLevel,
    SupportLevelSeries,
    TrendAssessment,
)
from app.domains.shared.entities import Candle, CandleSeries


class CandleResponse(BaseModel):
    time: int
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    direction: str  # "up" (green) or "down" (red)

    @classmethod
    def from_candle(cls, candle: Candle) -> "CandleResponse":
        return cls(
            time=int(candle.timestamp.timestamp()),
            timestamp=candle.timestamp,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            direction="up" if candle.is_bullish else "down",
        )


class CandleSeriesResponse(BaseModel):
    symbol: str
    timeframe: str
    count: int
    candles: list[CandleResponse]

    @classmethod
    def from_series(cls, series: CandleSeries) -> "CandleSeriesResponse":
        return cls(
            symbol=series.symbol,
            timeframe=series.timeframe.value,
            count=len(series.candles),
            candles=[CandleResponse.from_candle(c) for c in series.candles],
        )


class EmaPointResponse(BaseModel):
    time: int
    timestamp: datetime
    value: float


class EmaLineResponse(BaseModel):
    period: int
    count: int
    latest: float | None = None
    points: list[EmaPointResponse]

    @classmethod
    def from_line(cls, line: EmaLine) -> "EmaLineResponse":
        return cls(
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


class EmaResponse(BaseModel):
    symbol: str
    timeframe: str
    lines: list[EmaLineResponse]

    @classmethod
    def from_ema(cls, series: EmaSeries) -> "EmaResponse":
        return cls(
            symbol=series.symbol,
            timeframe=series.timeframe.value,
            lines=[EmaLineResponse.from_line(line) for line in series.lines],
        )


class SupportLevelResponse(BaseModel):
    price: float
    touches: int
    last_touched: date
    strength: str  # "weak" | "moderate" | "strong"
    distance_percent: float

    @classmethod
    def from_level(cls, level: SupportLevel) -> "SupportLevelResponse":
        return cls(
            price=level.price,
            touches=level.touches,
            last_touched=level.last_touched,
            strength=level.strength.value,
            distance_percent=level.distance_percent,
        )


class SupportLevelsResponse(BaseModel):
    symbol: str
    timeframe: str
    reference_price: float
    count: int
    levels: list[SupportLevelResponse]

    @classmethod
    def from_support_levels(cls, series: SupportLevelSeries) -> "SupportLevelsResponse":
        return cls(
            symbol=series.symbol,
            timeframe=series.timeframe.value,
            reference_price=series.reference_price,
            count=len(series.levels),
            levels=[SupportLevelResponse.from_level(level) for level in series.levels],
        )


class HorizonTrendResponse(BaseModel):
    period: int
    lookback: int
    direction: str  # "up" | "down" | "sideways" (EMA slope)
    effective_direction: str  # "up" | "down" | "sideways" (slope folded with price side)
    slope_percent: float
    change_percent: float
    price_vs_ema_percent: float
    ema: float

    @classmethod
    def from_horizon(cls, horizon: HorizonTrend | None) -> "HorizonTrendResponse | None":
        if horizon is None:
            return None
        return cls(
            period=horizon.period,
            lookback=horizon.lookback,
            direction=horizon.direction.value,
            effective_direction=horizon.effective_direction.value,
            slope_percent=horizon.slope_percent,
            change_percent=horizon.change_percent,
            price_vs_ema_percent=horizon.price_vs_ema_percent,
            ema=horizon.ema,
        )


class TrendResponse(BaseModel):
    symbol: str
    timeframe: str
    reference_price: float
    reading: str
    short_term: HorizonTrendResponse | None = None
    medium_term: HorizonTrendResponse | None = None
    long_term: HorizonTrendResponse | None = None

    @classmethod
    def from_assessment(cls, assessment: TrendAssessment) -> "TrendResponse":
        return cls(
            symbol=assessment.symbol,
            timeframe=assessment.timeframe.value,
            reference_price=assessment.reference_price,
            reading=assessment.reading.value,
            short_term=HorizonTrendResponse.from_horizon(assessment.short_term),
            medium_term=HorizonTrendResponse.from_horizon(assessment.medium_term),
            long_term=HorizonTrendResponse.from_horizon(assessment.long_term),
        )


class IndicatorPointResponse(BaseModel):
    time: int
    timestamp: datetime
    value: float


class IndicatorLineResponse(BaseModel):
    key: str
    count: int
    latest: float | None = None
    points: list[IndicatorPointResponse]

    @classmethod
    def from_line(cls, line: IndicatorLine) -> "IndicatorLineResponse":
        return cls(
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


class IndicatorResponse(BaseModel):
    name: str
    label: str
    overlay: bool
    lines: list[IndicatorLineResponse]

    @classmethod
    def from_indicator(cls, indicator: Indicator) -> "IndicatorResponse":
        return cls(
            name=indicator.name,
            label=indicator.label,
            overlay=indicator.overlay,
            lines=[IndicatorLineResponse.from_line(line) for line in indicator.lines],
        )


class IndicatorsResponse(BaseModel):
    symbol: str
    timeframe: str
    count: int
    indicators: list[IndicatorResponse]

    @classmethod
    def from_indicator_set(cls, result: IndicatorSet) -> "IndicatorsResponse":
        return cls(
            symbol=result.symbol,
            timeframe=result.timeframe.value,
            count=len(result.indicators),
            indicators=[
                IndicatorResponse.from_indicator(indicator)
                for indicator in result.indicators
            ],
        )
