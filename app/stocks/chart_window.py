"""Transport convenience: turn a chart 'range' preset into a time window.

A range like "6M" is a UX affordance ("show me the last six months"), not a
domain concept — so it lives here at the edge, next to the controller, rather
than in the use case. `now` is injected so the mapping stays pure and testable
(no hidden clock).
"""

from datetime import datetime, timedelta
from enum import Enum


class ChartRange(str, Enum):
    """How far back the chart should reach. String values double as the API's
    accepted ``range`` query values."""

    DAY_1 = "1D"
    DAY_7 = "7D"
    MONTH_1 = "1M"
    MONTH_3 = "3M"
    MONTH_6 = "6M"
    YEAR_1 = "1Y"
    YEAR_2 = "2Y"
    YEAR_5 = "5Y"
    YTD = "YTD"
    MAX = "MAX"


# Calendar-day lookbacks. Deliberately a touch generous (e.g. 1Y -> 366d) so a
# range never clips the far edge of the data it names.
_LOOKBACKS: dict[ChartRange, timedelta] = {
    ChartRange.DAY_1: timedelta(days=1),
    ChartRange.DAY_7: timedelta(days=7),
    ChartRange.MONTH_1: timedelta(days=31),
    ChartRange.MONTH_3: timedelta(days=92),
    ChartRange.MONTH_6: timedelta(days=183),
    ChartRange.YEAR_1: timedelta(days=366),
    ChartRange.YEAR_2: timedelta(days=731),
    ChartRange.YEAR_5: timedelta(days=1827),
}


def resolve_window(
    range_: ChartRange, *, now: datetime
) -> tuple[datetime | None, datetime]:
    """Return the ``(start, end)`` window for a range preset.

    ``start`` is ``None`` for ``MAX`` — meaning "as far back as the data goes".
    ``end`` is always ``now``.
    """
    if range_ is ChartRange.MAX:
        return None, now
    if range_ is ChartRange.YTD:
        year_start = now.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return year_start, now
    return now - _LOOKBACKS[range_], now
