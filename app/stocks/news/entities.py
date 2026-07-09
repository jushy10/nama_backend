"""Entities: a stock's recent news headlines.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than
reaching into the shared ``app/stocks/entities.py``, the same convention as the
earnings and recommendations sub-slices). Pure and vendor-agnostic — stdlib only.
They model a stock's news as a time series of articles: each ``NewsArticle`` is one
published story (or video), and ``StockNews`` is the run of articles for a symbol,
newest first.

Deliberately thin on computed rules — a news article carries few intrinsic facts to
derive (unlike an earnings surprise or a recommendation consensus). The one that is a
fact about the article, ``is_video``, lives here; ordering (newest first) is a promise
the adapter and repository keep when they build the tuple.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NewsArticle:
    """One published news item about a stock.

    ``id`` is the source's stable article id (Yahoo's UUID) — the identity the cache
    keys and dedupes on, so the same story fetched twice is one row. ``published_at``
    is when the item went out (timezone-aware UTC), the field the run is ordered by.
    Everything past ``title`` is best-effort enrichment the source may omit:
    ``publisher`` (the outlet), ``link`` (the article URL), ``summary`` (a plain-text
    blurb), ``content_type`` (``STORY`` / ``VIDEO``), and ``thumbnail_url``.
    """

    id: str
    title: str
    published_at: datetime
    publisher: str | None = None
    link: str | None = None
    summary: str | None = None
    content_type: str | None = None
    thumbnail_url: str | None = None

    @property
    def is_video(self) -> bool:
        """True when the item is a video rather than a written story — a fact about
        the item the presenter surfaces so a client can badge (or filter) the two."""
        return (self.content_type or "").upper() == "VIDEO"


@dataclass(frozen=True)
class StockNews:
    """A run of news articles for one symbol, newest first.

    Ordered newest-first like the earnings/recommendations histories, so ``latest`` is
    the front of the run. Best-effort: a symbol the source carries no news for yields an
    empty (``is_empty``) run, not an error.
    """

    symbol: str
    articles: tuple[NewsArticle, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when no article is carried (the source has no news for the symbol)."""
        return not self.articles

    @property
    def latest(self) -> NewsArticle | None:
        """The most recent article, or ``None`` when there's no news."""
        return self.articles[0] if self.articles else None
