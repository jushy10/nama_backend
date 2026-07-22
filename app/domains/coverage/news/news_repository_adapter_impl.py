from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.domains.coverage.news import models
from app.domains.coverage.news.entities import NewsArticle, StockNews
from app.domains.coverage.news.models import StockNewsRecord
from app.domains.coverage.news.interfaces import NewsRepositoryAdapter, RefreshTarget

# The feed is capped per stock so the higher-volume news history stays bounded (unlike
# the recommendations cache, which accumulates a slow monthly series unpruned). A stock
# briefly holds up to this many + one fetch's worth of rows, then prunes back to this.
_MAX_STORED_ARTICLES = 50


def _to_entity(row: StockNewsRecord) -> NewsArticle:
    return NewsArticle(
        id=row.article_id,
        title=row.title,
        published_at=row.published_at,
        publisher=row.publisher,
        link=row.link,
        summary=row.summary,
        content_type=row.content_type,
        thumbnail_url=row.thumbnail_url,
    )


def _to_news(symbol: str, rows: list[StockNewsRecord]) -> StockNews:
    articles = sorted(
        (_to_entity(row) for row in rows),
        key=lambda a: a.published_at,
        reverse=True,
    )
    return StockNews(symbol=symbol, articles=tuple(articles))


class NewsRepositoryAdapterImpl(NewsRepositoryAdapter):
    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> StockNews | None:
        rows = models.articles_by_symbol(self._session, symbol)
        if not rows:
            return None
        return _to_news(symbol, rows)

    def upsert(self, symbol: str, name: str | None, news: StockNews) -> None:
        stock = models.get_or_create_stock(self._session, symbol, name)

        # Merge, don't rewrite: clear only the articles the source served this time (by
        # id), then insert the fresh rows. A published article is a frozen fact and the
        # source serves only its latest handful, so earlier stored articles stay — the
        # feed accumulates beyond the source window.
        article_ids = [article.id for article in news.articles]
        models.delete_articles_for_ids(self._session, stock.id, article_ids)
        now = self._now()
        for article in news.articles:
            self._session.add(
                StockNewsRecord(
                    stock_id=stock.id,
                    article_id=article.id,
                    title=article.title,
                    publisher=article.publisher,
                    link=article.link,
                    summary=article.summary,
                    content_type=article.content_type,
                    thumbnail_url=article.thumbnail_url,
                    published_at=article.published_at,
                    fetched_at=now,
                )
            )
        # Cap the accumulated feed so it stays bounded (news volume dwarfs the monthly
        # recommendation series). Prune after the insert so the just-fetched articles are
        # in the running when the newest N are chosen.
        models.prune_to_newest(self._session, stock.id, _MAX_STORED_ARTICLES)
        self._session.commit()

    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        # Delegates the query to models (un-cached first, then least-recently-refreshed);
        # this layer just wraps each (symbol, name) pair in the domain-facing RefreshTarget.
        return [
            RefreshTarget(symbol, name)
            for symbol, name in models.stalest_symbols(self._session, limit)
        ]
