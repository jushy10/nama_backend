"""The news slice's composition root — the endpoint and the cron runner call build_*(db)
and receive a finished use case; all construction knowledge lives here."""

from functools import lru_cache

from sqlalchemy.orm import Session

from app.adapters.db.db_cached_news_adapter_impl import (
    NewsAdapterImpl as DbCachedNewsAdapterImpl,
)
from app.adapters.yfinance.news_adapter_impl import (
    NewsAdapterImpl as YfinanceNewsAdapterImpl,
)
from app.domains.coverage.news.db_repository import DbNewsRepository
from app.domains.coverage.news.interfaces import NewsAdapter
from app.domains.coverage.news.use_cases import GetStockNews, SyncStockNews


@lru_cache(maxsize=1)
def get_live_news_provider() -> NewsAdapter:
    # One process-singleton live provider (no key, no connection pool to share); the DB
    # cache that wraps it is built per request, since it needs the request session.
    return YfinanceNewsAdapterImpl()


def build_get_stock_news(db: Session) -> GetStockNews:
    # A persistent DB cache (refreshed out of band by the news cron + lazily on a miss)
    # sits in front of Yahoo so the read rarely calls it, and it serves stored rows
    # without a live round-trip. yfinance needs no key, so this is always wired.
    cached = DbCachedNewsAdapterImpl(get_live_news_provider(), DbNewsRepository(db))
    return GetStockNews(cached)


def build_sync_stock_news(db: Session) -> SyncStockNews:
    # The sweep talks to Yahoo directly — refreshing the stored rows is its whole point.
    return SyncStockNews(get_live_news_provider(), DbNewsRepository(db))
