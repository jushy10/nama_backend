"""HTTP response DTOs for the news endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. ``is_video`` is
surfaced as a plain field (it's computed on the entity) so a client doesn't have to
re-derive it from ``content_type``.
"""

from datetime import datetime

from pydantic import BaseModel


class NewsArticleResponse(BaseModel):
    """One published news item about a stock.

    ``id`` is the source's stable article id; ``published_at`` is when it went out
    (UTC). Everything past ``title`` is best-effort and may be ``null``. ``is_video``
    flags a video item (vs. a written story) so a client can badge or filter it."""

    id: str
    title: str
    published_at: datetime
    publisher: str | None = None
    link: str | None = None
    summary: str | None = None
    content_type: str | None = None
    thumbnail_url: str | None = None
    is_video: bool = False


class StockNewsResponse(BaseModel):
    """Recent news for a symbol, newest article first.

    ``latest`` is the most recent article and ``count`` how many are returned; an empty
    ``articles`` means the source carries no news for the symbol."""

    symbol: str
    count: int
    latest: NewsArticleResponse | None = None
    articles: list[NewsArticleResponse]
