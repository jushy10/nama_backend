"""HTTP response DTOs for the earnings-calendar endpoint.

Pydantic models kept at the edge, separate from the ``entities`` so the domain stays
framework-agnostic. ``when`` is the item's scheduled report date and ``session`` its market
timing relative to the US session ("bmo" before open / "amc" after close / "during" intraday /
"unknown"), both surfaced per item so a client that flattens the day groups still knows each
report's date and timing. The envelope carries the (clamped) ``from``/``to`` window and a total
``count`` alongside the ``days``, plus a service-authored ``disclaimer`` (informational, not
financial advice).
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class EarningsCalendarItemResponse(BaseModel):
    """One scheduled report: the company, the date it's expected to report (``when``), its
    market timing (``session``: bmo / amc / during / unknown), and its ``market_cap`` (whole
    units of the listing's currency; ``None`` for a not-yet-screened symbol) so a client can
    surface the large-cap names."""

    ticker: str
    name: str | None = None
    sector: str | None = None
    when: date
    session: str
    market_cap: float | None = None


class EarningsCalendarDayResponse(BaseModel):
    """The reports scheduled for one calendar date, alphabetical by ticker."""

    date: date
    count: int
    items: list[EarningsCalendarItemResponse]


class EarningsCalendarResponse(BaseModel):
    """Upcoming earnings grouped by day over the requested (clamped) window.

    ``from``/``to`` echo the window actually read (after clamping); ``count`` is the total
    reports across every day; ``days`` are only the days with at least one scheduled report."""

    model_config = ConfigDict(populate_by_name=True)

    from_: date = Field(alias="from")
    to: date
    count: int
    days: list[EarningsCalendarDayResponse]
    disclaimer: str
