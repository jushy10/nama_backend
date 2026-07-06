"""Entities: the top-ETFs view of a US exchange-traded fund.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than reaching
into the shared ``app/stocks/entities.py`` or the stock ``universe`` slice's — the same
convention as the earnings and recommendations sub-slices). Pure and vendor-agnostic —
stdlib only.

``ScreenedEtf`` is one row of what the *bulk screen* carries: the identity facts (``ticker`` /
``name`` / ``exchange``) alongside ``net_assets`` (assets under management, the ETF analogue of
a stock's market cap and the natural "top" ranking) and ``expense_ratio``. The fund's
``category`` is deliberately *not* on it — the bulk screen doesn't publish one, exactly like the
stock screen carries no sector — so it's filled separately by the sync's enrichment pass and
modelled as ``EtfClassification``.

The read side (``GET /stocks/etfs`` + ``GET /stocks/etfs/categories``) adds the shapes the
search flows through: ``EtfSearchCriteria`` (a normalized query — free text, a ``category``
filter, an ``EtfSort`` field with a ``SortDirection`` and a limit/offset page), the
``EtfSearchResult`` rows it matches wrapped in an ``EtfSearchPage`` (carrying the total match
count for pagination), and ``EtfCategories`` (the distinct category slugs the FE offers as a
filter menu). All pure value objects — the SQL that reads them lives in the adapter, the
normalization in the use case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class ScreenedEtf:
    """One fund in the screened top-ETF set — the facts the *bulk screen* carries.

    ``net_assets`` is assets under management in whole dollars (e.g. ``7.84e11`` for a $784B
    fund) — the fund's size, and the default "top" ranking. ``expense_ratio`` is a percent
    (``0.39`` = 0.39% a year). Everything but the ``ticker`` is optional: ``exchange`` and the
    name come from the screen, and either figure the screen omits rides in ``None``. The fund's
    ``category`` is not here — the screen doesn't carry it; the enrichment pass fills it (see
    ``EtfClassification``).
    """

    ticker: str
    name: str | None = None
    exchange: str | None = None
    net_assets: float | None = None
    expense_ratio: float | None = None


@dataclass(frozen=True)
class EtfClassification:
    """A fund's category, as a canonical snake_case slug.

    The screen (``ScreenedEtf``) doesn't carry it — Yahoo publishes the fund category only on
    the per-ticker ``.info`` surface — so this is the shape the sync's enrichment pass fetches
    and persists. ``category`` is ``None`` when Yahoo doesn't categorise the fund (left for a
    later run).

    Stored as a slug — lower-cased, with every run of non-alphanumeric characters collapsed to a
    single underscore (``"Large Growth"`` → ``large_growth``, ``"Commodities Focused"`` →
    ``commodities_focused``) — a stable, join-friendly key rather than Yahoo's display text.
    ``from_label`` is the constructor callers use, so the slug rule lives in one place.
    """

    category: str | None = None

    @classmethod
    def from_label(cls, category: object) -> "EtfClassification":
        """Build a classification from a raw vendor label, slugged to snake_case (dropped to
        ``None`` when blank or non-string)."""
        return cls(category=slugify(category))


class EtfSort(str, Enum):
    """The sortable columns of an ETF search.

    A ``str`` enum so FastAPI binds it straight from the ``?sort=`` query param (an unknown
    value is a 422, like ``StockSort``) and it serialises back as its value. ``NET_ASSETS`` is
    the natural default (biggest fund first — the "top" ETFs); ``EXPENSE_RATIO`` sorts by cost
    (cheapest first with ``order=asc``). Category is a *filter*, not a sort — it's a label, not a
    number. The value → column mapping is the adapter's job.
    """

    NET_ASSETS = "net_assets"
    EXPENSE_RATIO = "expense_ratio"


class SortDirection(str, Enum):
    """Ascending or descending — the ``?order=`` query param, bound the same way.

    Slice-local (the stock ``universe`` slice keeps its own copy) so this slice stays
    self-contained rather than importing another slice's entities.
    """

    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True)
class EtfSearchResult:
    """One row of an ETF search — the facts served straight from the ``etfs`` table, no live
    price (a page is a single DB read; the FE fetches a live quote per row on demand via the
    shared ``GET /stocks/{symbol}/quote``, which serves ETFs too).

    Everything but the ``ticker`` is nullable — a screened ETF always has ``net_assets`` (the
    screen's selection figure) but may still lack a name, an expense ratio, or a ``category``
    until the enrichment pass reaches it (or forever, for a fund Yahoo doesn't categorise).
    """

    ticker: str
    name: str | None
    exchange: str | None
    net_assets: float | None
    expense_ratio: float | None
    category: str | None


@dataclass(frozen=True)
class EtfSearchCriteria:
    """A normalized ETF-search request — the shape the use case hands the repository.

    Every field is already cleaned at the use-case edge: ``query`` is trimmed (``None`` when
    blank) and matched as a case-insensitive substring against name *or* ticker; ``category`` is
    slugged to the stored convention (``None`` when blank = don't filter); ``limit`` is clamped
    to a sane page and ``offset`` floored at zero. The adapter turns this into one SQL query.
    """

    query: str | None
    category: str | None
    sort: EtfSort
    direction: SortDirection
    limit: int
    offset: int


@dataclass(frozen=True)
class EtfSearchPage:
    """A page of search results plus the total number of matches.

    ``total`` is the full count *before* ``limit``/``offset`` (so the FE can render pagers);
    ``results`` is just this page. ``limit`` / ``offset`` echo the criteria the page was cut
    with, so a client reading only the response knows where it is.
    """

    results: tuple[EtfSearchResult, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class EtfCategories:
    """The distinct ETF category slugs present in the stored set — the FE's filter menu.

    One flat, sorted, de-duplicated list (nulls excluded); the search endpoint accepts the same
    slugs back as its ``category`` filter.
    """

    categories: tuple[str, ...]


def slugify(label: object) -> str | None:
    """A raw category label → a snake_case slug, or ``None``.

    Lower-cases, replaces each run of non-alphanumeric characters with a single ``_`` and strips
    leading/trailing underscores, turning display text into a stable key. A non-string or a
    label with no alphanumeric content (``""``, ``"—"``) collapses to ``None``. Idempotent on an
    already-slugged value, so the search use case can run an incoming ``category`` filter through
    it whether the client sends the raw label or the stored slug."""
    if not isinstance(label, str):
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug or None
