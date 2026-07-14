"""Entities: the daily market brief and the market snapshot it's written from.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than reaching
into the shared ``app/stocks/entities.py``, the same convention as the news / earnings /
recommendations sub-slices). Pure and vendor-agnostic — stdlib only.

Two shapes live here:

* ``MarketBrief`` — the stored artifact: one dated, AI-written read of the market, a
  ``tone`` + ``summary`` + an ordered list of ``MarketBriefSection``s (heading + body).
  ``is_complete`` is the intrinsic "worth storing?" rule the generate use case gates on.
* ``MarketBriefContext`` (+ ``BriefIndexMove`` / ``BriefSectorMove`` / ``BriefMover``) — the
  market snapshot the model reasons over, expressed in *brief-local* value objects so the
  port and the Bedrock adapter never depend on the market / heat-map slices' own entities.
  The generate use case maps those slices' reads onto these before handing them to the
  model, the same way the Bedrock market-summary adapter joins real board numbers into its
  own value objects.

The numbers on the context are always true quotes (joined from the live boards / the
screened universe); the model only contributes prose. So a figure in a brief is never
something the model recalled or invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class BriefTone(str, Enum):
    """The market's mood the day's moves imply — the brief's headline posture.

    The same three-way read the market-summary / sector-analysis AI uses: ``risk_on`` when
    the market is broadly rising (growth leading), ``risk_off`` when it's falling or leaning
    defensive, ``mixed`` when there's no clear lean. Kept slice-local (rather than importing
    the analysis slice's ``MarketTone``) so the brief slice stays self-contained.
    """

    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    MIXED = "mixed"


@dataclass(frozen=True)
class MarketBriefSection:
    """One section of the brief: a short ``heading`` and a plain-language ``body``.

    The brief is a handful of these in reading order (e.g. an overview, the sector rotation,
    the movers, what to watch) — an open list rather than fixed fields, so the model can
    shape the day's story without the schema dictating it. Both are model-authored prose.
    """

    heading: str
    body: str


@dataclass(frozen=True)
class MarketBrief:
    """One day's market brief — the stored, served artifact.

    ``brief_date`` is the calendar day the brief covers (its primary key in the store);
    ``generated_at`` is when it was written (UTC); ``tone`` is the headline posture;
    ``summary`` is the 2-3 sentence lede; ``sections`` are the ordered body sections; and
    ``model`` records which model produced it. One row per date — a brief is a durable,
    dated fact, never regenerated on a read.
    """

    brief_date: date
    generated_at: datetime
    tone: BriefTone
    summary: str
    sections: tuple[MarketBriefSection, ...]
    model: str

    @property
    def is_complete(self) -> bool:
        """Whether the brief is worth storing: a non-empty summary AND at least one section.

        The generate use case only upserts a *complete* brief, so a rare hollow model result
        (a summary with no sections, or vice versa) is never frozen into the store — the next
        day's run simply tries again."""
        return bool(self.summary and self.sections)


@dataclass(frozen=True)
class BriefIndexMove:
    """One headline index's move, as the brief reads it: the day plus trailing windows.

    A brief-local projection of the market slice's ``MarketIndexPerformance`` — only the
    figures the model narrates (today / past week / month / year), each ``None`` when the
    board carried no quote for that window.
    """

    name: str
    symbol: str
    change_percent: float | None
    one_week: float | None
    one_month: float | None
    one_year: float | None


@dataclass(frozen=True)
class BriefSectorMove:
    """One sector's move on the day, as the brief reads it — the sector's proxy-ETF change."""

    sector: str
    symbol: str
    change_percent: float | None


@dataclass(frozen=True)
class BriefMover:
    """One stock among the day's biggest movers: its ticker, name, sector and day change."""

    ticker: str
    name: str | None
    sector: str | None
    change_percent: float


@dataclass(frozen=True)
class BriefHeadline:
    """One recent news headline the brief can cite as a catalyst for the day's moves.

    Carried straight from the news slice (DB-only, never a live fetch), so ``title`` /
    ``publisher`` / ``published_at`` are the stored article's own facts — the model may
    reference the headline as a reason but never authors it. ``ticker`` is the mover the
    headline belongs to, and ``publisher`` the outlet that ran it.
    """

    ticker: str
    title: str
    publisher: str | None = None
    published_at: datetime | None = None


@dataclass(frozen=True)
class MarketBriefContext:
    """The market snapshot the model writes the brief from — all true quotes, no prose.

    ``indexes`` and ``sectors`` are the headline boards; ``gainers`` / ``losers`` are the
    day's extremes (largest up / down moves) across the covered universe; ``advancers`` /
    ``decliners`` / ``quoted`` are the day's breadth (how many stocks rose vs fell of those
    with a live quote); ``headlines`` are recent news outlets' headlines about the day's
    movers — the "why" behind the moves, read DB-only from the news store. Every leg is
    best-effort on the way in, so any of them may be empty — ``has_data`` is the "is there
    enough to write a brief at all?" gate the use case checks before spending a model call.
    """

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
        """True when there's a headline board to write from (indices or sectors).

        The movers/breadth ride on best-effort live quotes and can be empty on an off day
        without meaning "no market"; the index/sector boards are the real signal, so their
        absence is what says "gathered nothing — don't bother the model"."""
        return bool(self.indexes or self.sectors)
