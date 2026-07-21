from dataclasses import dataclass
from datetime import date

# The two tenors the 2s10s spread is read off of. Named so the entity's spread
# logic and the history slice agree on one spelling.
_TWO_YEAR = "2Y"
_TEN_YEAR = "10Y"


@dataclass(frozen=True)
class YieldTenor:
    label: str  # human tenor, e.g. "1M", "2Y", "10Y"
    months: float  # tenor in months, for ordering the curve
    rate: float  # annualized par yield, percent


@dataclass(frozen=True)
class YieldCurve:
    as_of: date
    tenors: tuple[YieldTenor, ...]

    def _rate(self, label: str) -> float | None:
        for tenor in self.tenors:
            if tenor.label == label:
                return tenor.rate
        return None

    @property
    def two_year(self) -> float | None:
        return self._rate(_TWO_YEAR)

    @property
    def ten_year(self) -> float | None:
        return self._rate(_TEN_YEAR)

    @property
    def spread_2s10s(self) -> float | None:
        two, ten = self.two_year, self.ten_year
        if two is None or ten is None:
            return None
        return round(ten - two, 2)

    @property
    def is_inverted(self) -> bool | None:
        spread = self.spread_2s10s
        return None if spread is None else spread < 0


@dataclass(frozen=True)
class YieldObservation:
    on: date
    rate: float  # annualized yield, percent


@dataclass(frozen=True)
class YieldSeries:
    label: str  # the maturity, e.g. "2Y" / "10Y"
    observations: tuple[YieldObservation, ...]


@dataclass(frozen=True)
class YieldHistory:
    series: tuple[YieldSeries, ...]

    def _by_label(self, label: str) -> YieldSeries | None:
        for series in self.series:
            if series.label == label:
                return series
        return None

    @property
    def spread(self) -> tuple[YieldObservation, ...]:
        two = self._by_label(_TWO_YEAR)
        ten = self._by_label(_TEN_YEAR)
        if two is None or ten is None:
            return ()
        ten_by_date = {obs.on: obs.rate for obs in ten.observations}
        return tuple(
            YieldObservation(on=obs.on, rate=round(ten_by_date[obs.on] - obs.rate, 2))
            for obs in two.observations
            if obs.on in ten_by_date
        )

    @property
    def latest_spread(self) -> float | None:
        spread = self.spread
        return spread[-1].rate if spread else None

    @property
    def is_inverted(self) -> bool | None:
        latest = self.latest_spread
        return None if latest is None else latest < 0
