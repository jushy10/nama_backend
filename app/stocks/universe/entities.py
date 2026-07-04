"""Entities: the investable-universe view of a stock.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than reaching
into the shared ``app/stocks/entities.py``, the same convention as the earnings and
recommendations sub-slices). Pure and vendor-agnostic — stdlib only.

``ScreenedStock`` is one row of the screened universe: the identity facts the ``stocks``
anchor holds (``ticker`` / ``name`` / ``exchange``) alongside the screen's own figures —
``market_cap`` (the selection criterion) and ``sector``. It is the single shape the
screener returns and the sync persists onto the anchor.

``CompanyClassification`` is the stock's sector + industry, fetched separately (the bulk
screen carries neither) and stored as snake_case slugs by the sync's enrichment pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ScreenedStock:
    """One company in the screened universe.

    ``market_cap`` is in whole dollars (e.g. ``3.01e12`` for a $3.01T company). Everything
    but the ``ticker`` is optional: ``exchange`` comes from the screen, ``sector`` may be
    absent (the yfinance screen doesn't publish it, so it rides in ``None``), and the name
    may be missing.
    """

    ticker: str
    name: str | None = None
    exchange: str | None = None
    market_cap: float | None = None
    sector: str | None = None


@dataclass(frozen=True)
class CompanyClassification:
    """A stock's sector + industry, as canonical snake_case slugs.

    The screen (``ScreenedStock``) carries neither — Yahoo publishes sector/industry only on
    the per-ticker ``.info`` surface — so this is the shape the sync's enrichment pass fetches
    and persists. Both sides are optional: a symbol Yahoo doesn't classify (or only half
    classifies) yields ``None`` for the missing side, which the sync leaves for a later run.

    Labels are stored as slugs — lower-cased, with every run of non-alphanumeric characters
    collapsed to a single underscore (``"Consumer Electronics"`` → ``consumer_electronics``,
    ``"Oil & Gas E&P"`` → ``oil_gas_e_p``) — a stable, join-friendly key rather than Yahoo's
    display text. ``from_labels`` is the constructor callers use, so the slug rule lives in
    one place.
    """

    sector: str | None = None
    industry: str | None = None

    @classmethod
    def from_labels(cls, sector: object, industry: object) -> "CompanyClassification":
        """Build a classification from raw vendor labels, each slugged to snake_case (and
        dropped to ``None`` when blank or non-string)."""
        return cls(sector=_slugify(sector), industry=_slugify(industry))


def _slugify(label: object) -> str | None:
    """A raw classification label → a snake_case slug, or ``None``.

    Lower-cases, replaces each run of non-alphanumeric characters with a single ``_`` and
    strips leading/trailing underscores, turning display text into a stable key. A non-string
    or a label with no alphanumeric content (``""``, ``"—"``) collapses to ``None``."""
    if not isinstance(label, str):
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug or None
