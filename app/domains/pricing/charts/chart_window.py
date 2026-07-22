from datetime import datetime, timedelta
from enum import Enum


class ChartRange(str, Enum):
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
    if range_ is ChartRange.MAX:
        return None, now
    if range_ is ChartRange.YTD:
        year_start = now.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return year_start, now
    return now - _LOOKBACKS[range_], now
