from datetime import date, datetime

from pydantic import BaseModel


class CandleResponse(BaseModel):
    time: int
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    direction: str  # "up" (green) or "down" (red)


class CandleSeriesResponse(BaseModel):
    symbol: str
    timeframe: str
    count: int
    candles: list[CandleResponse]


class EmaPointResponse(BaseModel):
    time: int
    timestamp: datetime
    value: float


class EmaLineResponse(BaseModel):
    period: int
    count: int
    latest: float | None = None
    points: list[EmaPointResponse]


class EmaResponse(BaseModel):
    symbol: str
    timeframe: str
    lines: list[EmaLineResponse]


class SupportLevelResponse(BaseModel):
    price: float
    touches: int
    last_touched: date
    strength: str  # "weak" | "moderate" | "strong"
    distance_percent: float


class SupportLevelsResponse(BaseModel):
    symbol: str
    timeframe: str
    reference_price: float
    count: int
    levels: list[SupportLevelResponse]


class HorizonTrendResponse(BaseModel):
    period: int
    lookback: int
    direction: str  # "up" | "down" | "sideways" (EMA slope)
    effective_direction: str  # "up" | "down" | "sideways" (slope folded with price side)
    slope_percent: float
    change_percent: float
    price_vs_ema_percent: float
    ema: float


class TrendResponse(BaseModel):
    symbol: str
    timeframe: str
    reference_price: float
    reading: str
    short_term: HorizonTrendResponse | None = None
    medium_term: HorizonTrendResponse | None = None
    long_term: HorizonTrendResponse | None = None


class IndicatorPointResponse(BaseModel):
    time: int
    timestamp: datetime
    value: float


class IndicatorLineResponse(BaseModel):
    key: str
    count: int
    latest: float | None = None
    points: list[IndicatorPointResponse]


class IndicatorResponse(BaseModel):
    name: str
    label: str
    overlay: bool
    lines: list[IndicatorLineResponse]


class IndicatorsResponse(BaseModel):
    symbol: str
    timeframe: str
    count: int
    indicators: list[IndicatorResponse]
