"""Entities: how a company breaks its revenue down — *what* it makes money on.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than reaching
into the shared ``app/stocks/entities.py``, the same convention as the earnings /
recommendations / news sub-slices). Pure and vendor-agnostic — stdlib only.

Where the earnings slices carry the *total* reported revenue per period, this slice carries
its **disaggregation**: for a fiscal year, how much revenue came from each operating segment
(Google Services vs. Google Cloud), each product line (Search vs. YouTube vs. Cloud), and each
geography (US vs. EMEA vs. APAC). The three cuts are the three ``SegmentAxis`` values — the
axes a US filer reports its revenue disaggregation along in its 10-K (the XBRL segment note).

Two facts about the domain shape everything here:

- **Members are company-defined, not a shared vocabulary.** ``GoogleServicesMember`` is a fact
  about *Alphabet's* filing; there is no cross-company segment taxonomy the way there is for
  sectors. So ``member`` is stored raw (the filer's own label) and can be compared *within* a
  company over time but never aggregated *across* companies. ``label`` is a best-effort
  human-readable rendering of that raw member, derived (not stored) — see ``humanize_member``.
- **A reported year's disaggregation is a frozen fact.** Once a 10-K states FY2024's segment
  revenue it never changes, so the cache accumulates history across filings (each 10-K only
  restates its most-recent ~3 years) — the merge-preserving upsert, like recommendations/news.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum


class SegmentAxis(str, Enum):
    """The cut a revenue figure is disaggregated along — the three axes a US filer reports
    revenue by in its 10-K segment note.

    A ``str`` enum so the value serializes as its plain slug (``"product"``) and compares to
    strings, matching the sector/industry slug convention on the anchor. The adapter maps the
    filing's XBRL axis (``StatementBusinessSegmentsAxis`` / ``ProductOrServiceAxis`` /
    ``StatementGeographicalAxis``) onto these.
    """

    BUSINESS = "business_segment"  # operating segments (e.g. Google Services, Google Cloud)
    PRODUCT = "product"  # product / service lines (e.g. Search, YouTube ads)
    GEOGRAPHY = "geography"  # geographic markets (e.g. United States, EMEA)


@dataclass(frozen=True)
class RevenueSegment:
    """One revenue figure for one (fiscal year, axis, member) — e.g. "FY2024, product,
    Google Cloud → $58.7B".

    ``member`` is the filer's own raw XBRL member local-name (``GoogleCloudMember``): the
    identity a refresh keys on, comparable within a company but not across companies.
    ``label`` is derived from it for display, never stored. ``value`` is revenue in the
    filing's reporting currency (raw, typically USD — no currency field, matching the bare
    revenue floats the earnings slices store). ``period_end`` is the fiscal year end.
    """

    fiscal_year: int
    period_end: date | None
    axis: SegmentAxis
    member: str  # raw XBRL member local-name (the filer's label)
    value: float  # revenue, raw reporting currency (typically USD)

    @property
    def label(self) -> str:
        """A human-readable rendering of the raw ``member`` (``GoogleCloudMember`` ->
        ``"Google Cloud"``). Derived on access, not stored, so it's never stale and an
        improvement to ``humanize_member`` applies to every row at once."""
        return humanize_member(self.member)


@dataclass(frozen=True)
class RevenueSegmentation:
    """A company's revenue disaggregation — its segments across the fiscal years on file.

    ``segments`` holds every (year, axis, member) figure; the ``for_axis`` / ``latest_for_axis``
    views slice it into the cut a client wants. Best-effort: a company that reports no
    disaggregation (a single-segment filer, or a foreign issuer that files a 20-F we don't
    parse) yields an empty (``is_empty``) segmentation, not an error — the same contract the
    other best-effort slices use.
    """

    symbol: str
    segments: tuple[RevenueSegment, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when no segment figure is carried (the company reports no disaggregation)."""
        return not self.segments

    @property
    def fiscal_years(self) -> tuple[int, ...]:
        """The distinct fiscal years on file, newest first."""
        return tuple(sorted({s.fiscal_year for s in self.segments}, reverse=True))

    @property
    def latest_fiscal_year(self) -> int | None:
        """The most recent fiscal year with any segment data, or ``None`` when empty."""
        years = self.fiscal_years
        return years[0] if years else None

    def for_axis(self, axis: SegmentAxis) -> tuple[RevenueSegment, ...]:
        """Every segment on one axis, newest year first then largest value first."""
        return tuple(
            sorted(
                (s for s in self.segments if s.axis == axis),
                key=lambda s: (-s.fiscal_year, -s.value),
            )
        )

    def latest_for_axis(self, axis: SegmentAxis) -> tuple[RevenueSegment, ...]:
        """One axis's breakdown for the latest fiscal year that has it, largest value first —
        the "what did they make revenue on last year" view. Empty when the axis has no data.

        Uses each axis's *own* newest year, not the segmentation's overall latest: a filer may
        publish the product cut a year behind the geographic one, and this still returns the
        freshest available breakdown for whichever axis is asked."""
        rows = self.for_axis(axis)
        if not rows:
            return ()
        newest = rows[0].fiscal_year
        return tuple(s for s in rows if s.fiscal_year == newest)


def humanize_member(member: str) -> str:
    """Render a raw XBRL member local-name as a human label.

    ``GoogleServicesMember`` -> ``"Google Services"``, ``AllOtherSegmentsMember`` -> ``"All
    Other Segments"``, ``UnitedStatesMember`` -> ``"United States"``, ``EMEAMember`` ->
    ``"EMEA"``. Strips the conventional ``Member`` suffix and splits CamelCase (keeping
    all-caps runs like acronyms intact). Best-effort cosmetics — falls back to the raw member
    when there's nothing sensible to split."""
    base = member[:-6] if member.endswith("Member") else member
    # Split on CamelCase / acronym / digit boundaries: an acronym run that precedes a
    # capitalized word (EMEA|Region), a capitalized or lower word, a bare acronym run, or digits.
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", base)
    return " ".join(words) or member
