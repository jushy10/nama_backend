"""Interface Adapter: a read-through database cache in front of any NewsProvider.

The read path calls the database first; only on a **miss** (no stored rows for the symbol)
does it hit Yahoo, store the result, and return it. A symbol that already has rows is always
served straight from the DB — the read never re-fetches based on age. Keeping stored rows
current is entirely the out-of-band cron's job (``SyncStockNews``), which merges fresh
articles in on its schedule. This keeps the endpoint off Yahoo, which rate-limits and blocks
data-centre IPs.

It implements ``NewsProvider``, so it slots into the wiring exactly where the bare yfinance
provider would, with the use case none the wiser.

Resilience:

- A cache *read* failure (DB hiccup) is treated as a miss, so a database problem falls
  through to the live source rather than sinking the news.
- A cache *write* failure is swallowed: the caller still gets the freshly-fetched articles.
- An empty live result is not stored (there'd be nothing to store), so a symbol with no news
  simply re-checks the live source on its next view rather than being cached as empty.
"""

import logging

from app.stocks.news.entities import StockNews
from app.stocks.news.ports import NewsProvider
from app.stocks.news.repository import NewsRepository

logger = logging.getLogger(__name__)


class DbCachedNewsProvider(NewsProvider):
    """A read-through DB cache: serve stored rows, else fetch from the inner provider and store."""

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
