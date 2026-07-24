from datetime import datetime

from pydantic import BaseModel

from app.domains.coverage.news.entities import NewsArticle, StockNews


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

    @classmethod
    def from_article(cls, article: NewsArticle) -> "NewsArticleResponse":
        return cls(
            id=article.id,
            title=article.title,
            published_at=article.published_at,
            publisher=article.publisher,
            link=article.link,
            summary=article.summary,
            content_type=article.content_type,
            thumbnail_url=article.thumbnail_url,
            is_video=article.is_video,
        )


class StockNewsResponse(BaseModel):
    symbol: str
    count: int
    latest: NewsArticleResponse | None = None
    articles: list[NewsArticleResponse]

    @classmethod
    def from_news(cls, news: StockNews) -> "StockNewsResponse":
        latest = news.latest
        return cls(
            symbol=news.symbol,
            count=len(news.articles),
            latest=NewsArticleResponse.from_article(latest) if latest else None,
            articles=[NewsArticleResponse.from_article(a) for a in news.articles],
        )
