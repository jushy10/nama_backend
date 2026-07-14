"""Enterprise Business Rules: the Treasury-yields slice's own entities.

The US Treasury market's two headline reads: the **par-yield curve** (yields
across every maturity on one day) and the **2Y/10Y history** (each yield over
time). Pure domain objects — frozen dataclasses that import nothing from the
outer layers. The famous ``2s10s`` spread (10Y minus 2Y) and the ``inverted``
signal are facts *about* the data, so they live here as computed properties,
never stored.
"""

from dataclasses import dataclass
from datetime import date

# The two tenors the 2s10s spread is read off of. Named so the entity's spread
# logic and the history slice agree on one spelling.
_TWO_YEAR = "2Y"
_TEN_YEAR = "10Y"


@dataclass(frozen=True)
class YieldTenor:
    """One point on the yield curve: a maturity and its annualized par yield.

    ``months`` is the tenor expressed in months (1M -> 1, 2Y -> 24, 30Y -> 360)
    purely so the curve orders shortest-to-longest without parsing the label.
    ``rate`` is a percent (4.26 means 4.26%).
    """

    label: str  # human tenor, e.g. "1M", "2Y", "10Y"
    months: float  # tenor in months, for ordering the curve
    rate: float  # annualized par yield, percent


@dataclass(frozen=True)
class YieldCurve:
    """The US Treasury par-yield curve on one date — yields across maturities.

    ``tenors`` are ordered shortest maturity first. An upward curve (10Y above
    2Y) is the normal, healthy shape; an *inverted* one (2Y above 10Y) has
    preceded every US recession since the 1950s, which is why the ``2s10s``
    spread and ``is_inverted`` flag are surfaced as first-class reads.
    """

    as_of: date
    tenors: tuple[YieldTenor, ...]

    def _rate(self, label: str) -> float | None:
        for tenor in self.tenors:
            if tenor.label == label:
                return tenor.rate
        return None

    @property
    def two_year(self) -> float | None:
        """The 2-year par yield (percent), or None if the curve omits it."""
        return self._rate(_TWO_YEAR)

    @property
    def ten_year(self) -> float | None:
        """The 10-year par yield (percent), or None if the curve omits it."""
        return self._rate(_TEN_YEAR)

    @property
    def spread_2s10s(self) -> float | None:
        """10Y minus 2Y, in percentage points. Negative == inverted curve."""
        two, ten = self.two_year, self.ten_year
        if two is None or ten is None:
            return None
        return round(ten - two, 2)

    @property
    def is_inverted(self) -> bool | None:
        """Whether the curve is inverted (2Y above 10Y). None if either is missing."""
        spread = self.spread_2s10s
        return None if spread is None else spread < 0


@dataclass(frozen=True)
class YieldObservation:
    """One maturity's yield on one date — a single point in a history series."""

    on: date
    rate: float  # annualized yield, percent


@dataclass(frozen=True)
class YieldSeries:
    """One maturity's yield over time (e.g. the 2Y), oldest observation first."""

    label: str  # the maturity, e.g. "2Y" / "10Y"
    observations: tuple[YieldObservation, ...]


@dataclass(frozen=True)
class YieldHistory:
    """The 2Y and 10Y yields over time, plus their derived 2s10s spread.

    The spread series is computed on the dates the two maturities share — the
    day the spread crosses below zero is the moment the curve inverts, so a
    consumer can plot the crossover directly.
    """

    series: tuple[YieldSeries, ...]

    def _by_label(self, label: str) -> YieldSeries | None:
        for series in self.series:
            if series.label == label:
                return series
        return None

    @property
    def spread(self) -> tuple[YieldObservation, ...]:
        """The 2s10s (10Y - 2Y) on every date both maturities were quoted."""
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
        """The most recent 2s10s value, or None if the series can't be paired."""
        spread = self.spread
        return spread[-1].rate if spread else None

    @property
    def is_inverted(self) -> bool | None:
        """Whether the curve is inverted as of the latest paired observation."""
        latest = self.latest_spread
        return None if latest is None else latest < 0
