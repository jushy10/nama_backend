import datetime

from pydantic import BaseModel

from app.domains.macro.yields.entities import (
    YieldCurve,
    YieldHistory,
    YieldObservation,
    YieldSeries,
    YieldTenor,
)


class YieldTenorResponse(BaseModel):
    label: str
    months: float
    rate: float

    @classmethod
    def from_tenor(cls, tenor: YieldTenor) -> "YieldTenorResponse":
        return cls(label=tenor.label, months=tenor.months, rate=tenor.rate)


class YieldCurveResponse(BaseModel):
    as_of: datetime.date
    two_year: float | None = None
    ten_year: float | None = None
    # 10Y minus 2Y, in percentage points; negative == inverted curve.
    spread_2s10s: float | None = None
    is_inverted: bool | None = None
    count: int
    tenors: list[YieldTenorResponse]

    @classmethod
    def from_curve(cls, curve: YieldCurve) -> "YieldCurveResponse":
        return cls(
            as_of=curve.as_of,
            two_year=curve.two_year,
            ten_year=curve.ten_year,
            spread_2s10s=curve.spread_2s10s,
            is_inverted=curve.is_inverted,
            count=len(curve.tenors),
            tenors=[YieldTenorResponse.from_tenor(t) for t in curve.tenors],
        )


class YieldObservationResponse(BaseModel):
    date: datetime.date
    rate: float

    @classmethod
    def from_observation(cls, obs: YieldObservation) -> "YieldObservationResponse":
        return cls(date=obs.on, rate=obs.rate)


class YieldSeriesResponse(BaseModel):
    label: str
    observations: list[YieldObservationResponse]

    @classmethod
    def from_series(cls, series: YieldSeries) -> "YieldSeriesResponse":
        return cls(
            label=series.label,
            observations=[
                YieldObservationResponse.from_observation(o)
                for o in series.observations
            ],
        )


class YieldHistoryResponse(BaseModel):
    latest_spread: float | None = None
    is_inverted: bool | None = None
    series: list[YieldSeriesResponse]
    # The 10Y-2Y spread on every date both maturities were quoted; the point it
    # crosses below zero is where the curve inverts.
    spread: list[YieldObservationResponse]

    @classmethod
    def from_history(cls, history: YieldHistory) -> "YieldHistoryResponse":
        return cls(
            latest_spread=history.latest_spread,
            is_inverted=history.is_inverted,
            series=[YieldSeriesResponse.from_series(s) for s in history.series],
            spread=[
                YieldObservationResponse.from_observation(o) for o in history.spread
            ],
        )
