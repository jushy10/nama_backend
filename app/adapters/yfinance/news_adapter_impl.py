from __future__ import annotations

from datetime import datetime, timezone

import yfinance as yf

from app.adapters.yfinance import session
from app.domains.shared.exceptions import StockDataUnavailable
from app.domains.coverage.news.entities import NewsArticle, StockNews
from app.domains.coverage.news.interfaces import NewsAdapter


class NewsAdapterImpl(NewsAdapter):
    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to
        # the real thing.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_news(self, symbol: str) -> StockNews:
        try:
            # An empty list is how yfinance can surface a swallowed crumb 401, so retry
            # once with a fresh crumb; a genuine no-news symbol just comes back empty
            # after that.
            items = session.call(
                lambda: self._ticker_factory(symbol).news,
                is_empty=lambda result: not result,
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(symbol, f"yfinance news failed ({exc})") from exc
        return StockNews(symbol=symbol, articles=tuple(_parse_articles(items)))


def _parse_articles(items) -> list[NewsArticle]:
    if not items:
        return []
    seen: set[str] = set()
    articles: list[NewsArticle] = []
    for item in items:
        content = (item or {}).get("content") or {}
        raw_id = item.get("id") or content.get("id")
        title = _clean(content.get("title"))
        published = _parse_published(content.get("pubDate"))
        if not raw_id or not title or published is None:
            continue
        article_id = str(raw_id)
        if article_id in seen:
            continue
        seen.add(article_id)
        articles.append(
            NewsArticle(
                id=article_id,
                title=title,
                published_at=published,
                publisher=_provider_name(content),
                link=_url(content.get("canonicalUrl")) or _url(content.get("clickThroughUrl")),
                summary=_clean(content.get("summary")),
                content_type=_clean(content.get("contentType")),
                thumbnail_url=_url(content.get("thumbnail"), key="originalUrl"),
            )
        )
    articles.sort(key=lambda a: a.published_at, reverse=True)  # newest first
    return articles


def _parse_published(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # fromisoformat handles the trailing "Z" on 3.11+, but normalize it anyway for
        # older interpreters / odd offsets.
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _url(node, *, key: str = "url") -> str | None:
    if isinstance(node, str):
        return node or None
    if isinstance(node, dict):
        return _clean(node.get(key))
    return None


def _provider_name(content) -> str | None:
    provider = content.get("provider")
    if isinstance(provider, dict):
        return _clean(provider.get("displayName"))
    return None


def _clean(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
