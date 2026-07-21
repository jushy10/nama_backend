from datetime import datetime

from pydantic import BaseModel


class NewsArticleResponse(BaseModel):
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
    symbol: str
    count: int
    latest: NewsArticleResponse | None = None
    articles: list[NewsArticleResponse]
