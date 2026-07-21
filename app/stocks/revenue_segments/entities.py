from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum


class SegmentAxis(str, Enum):
    BUSINESS = "business_segment"  # operating segments (e.g. Google Services, Google Cloud)
    PRODUCT = "product"  # product / service lines (e.g. Search, YouTube ads)
    GEOGRAPHY = "geography"  # geographic markets (e.g. United States, EMEA)


@dataclass(frozen=True)
class RevenueSegment:
    fiscal_year: int
    period_end: date | None
    axis: SegmentAxis
    member: str  # raw XBRL member local-name (the filer's label)
    value: float  # revenue, raw reporting currency (typically USD)

    @property
    def label(self) -> str:
        return humanize_member(self.member)


@dataclass(frozen=True)
class RevenueSegmentation:
    symbol: str
    segments: tuple[RevenueSegment, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.segments

    @property
    def fiscal_years(self) -> tuple[int, ...]:
        return tuple(sorted({s.fiscal_year for s in self.segments}, reverse=True))

    @property
    def latest_fiscal_year(self) -> int | None:
        years = self.fiscal_years
        return years[0] if years else None

    def for_axis(self, axis: SegmentAxis) -> tuple[RevenueSegment, ...]:
        return tuple(
            sorted(
                (s for s in self.segments if s.axis == axis),
                key=lambda s: (-s.fiscal_year, -s.value),
            )
        )

    def latest_for_axis(self, axis: SegmentAxis) -> tuple[RevenueSegment, ...]:
        rows = self.for_axis(axis)
        if not rows:
            return ()
        newest = rows[0].fiscal_year
        return tuple(s for s in rows if s.fiscal_year == newest)


def humanize_member(member: str) -> str:
    base = member[:-6] if member.endswith("Member") else member
    # Split on CamelCase / acronym / digit boundaries: an acronym run that precedes a
    # capitalized word (EMEA|Region), a capitalized or lower word, a bare acronym run, or digits.
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", base)
    return " ".join(words) or member
