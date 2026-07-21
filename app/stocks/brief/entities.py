from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class BriefTone(str, Enum):
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    MIXED = "mixed"


@dataclass(frozen=True)
class MarketBriefSection:
    heading: str
    body: str


@dataclass(frozen=True)
class MarketBrief:
    brief_date: date
    generated_at: datetime
    tone: BriefTone
    summary: str
    sections: tuple[MarketBriefSection, ...]
    model: str

    @property
    def is_complete(self) -> bool:
        return bool(self.summary and self.sections)


@dataclass(frozen=True)
class BriefIndexMove:
    name: str
    symbol: str
    change_percent: float | None
    one_week: float | None
    one_month: float | None
    one_year: float | None


@dataclass(frozen=True)
class BriefSectorMove:
    sector: str
    symbol: str
    change_percent: float | None


@dataclass(frozen=True)
class BriefMover:
    ticker: str
    name: str | None
    sector: str | None
    change_percent: float


@dataclass(frozen=True)
class BriefHeadline:
    ticker: str
    title: str
    publisher: str | None = None
    published_at: datetime | None = None


@dataclass(frozen=True)
class MarketBriefContext:
    indexes: tuple[BriefIndexMove, ...] = ()
    sectors: tuple[BriefSectorMove, ...] = ()
    gainers: tuple[BriefMover, ...] = ()
    losers: tuple[BriefMover, ...] = ()
    advancers: int = 0
    decliners: int = 0
    quoted: int = 0
    headlines: tuple[BriefHeadline, ...] = ()

    @property
    def has_data(self) -> bool:
        return bool(self.indexes or self.sectors)
