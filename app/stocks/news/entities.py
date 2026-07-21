from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NewsArticle:
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
        return (self.content_type or "").upper() == "VIDEO"


@dataclass(frozen=True)
class StockNews:
    symbol: str
    articles: tuple[NewsArticle, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.articles

    @property
    def latest(self) -> NewsArticle | None:
        return self.articles[0] if self.articles else None
