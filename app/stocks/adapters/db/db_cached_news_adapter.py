import logging

from app.stocks.company.news.entities import StockNews
from app.stocks.company.news.ports import NewsProvider
from app.stocks.company.news.repository import NewsRepository

logger = logging.getLogger(__name__)


class DbCachedNewsProvider(NewsProvider):
    def __init__(self, inner: NewsProvider, repo: NewsRepository) -> None:
        self._inner = inner
        self._repo = repo

    def get_news(self, symbol: str) -> StockNews:
        stored = self._safe_get(symbol)
        if stored is not None:
            return stored  # a populated symbol is served straight from the DB, any age
        # Miss: nothing stored → fetch from the live source, store it, and return it. A
        # live failure here has nothing to fall back on, so it propagates (→ 502).
        news = self._inner.get_news(symbol)
        if not news.is_empty:
            self._safe_upsert(symbol, news)
        return news

    def _safe_get(self, symbol: str) -> StockNews | None:
        # A cache read must never break the news: on any error, treat it as a miss and let
        # the caller fall through to the live source.
        try:
            return self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning("news cache read failed for %s", symbol, exc_info=True)
            return None

    def _safe_upsert(self, symbol: str, news: StockNews) -> None:
        # Caching is best-effort; a write failure must not fail the request the caller
        # already has a good answer for. (Name comes from the sync job, not this feed, so
        # it's left untouched here.)
        try:
            self._repo.upsert(symbol, None, news)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning("news cache write failed for %s", symbol, exc_info=True)
