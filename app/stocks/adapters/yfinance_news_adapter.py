"""Interface Adapter: a stock's recent news from Yahoo Finance (via ``yfinance``).

``Ticker.news`` returns a small list of the stock's latest articles. Recent yfinance
wraps each item as ``{"id": ..., "content": {...}}``, with the interesting fields nested
under ``content``: ``title``, ``summary``, ``pubDate`` (ISO-8601 UTC), ``contentType``
(``STORY`` / ``VIDEO``), ``provider.displayName`` (the outlet), ``canonicalUrl`` /
``clickThroughUrl`` (the article link, each a ``{"url": ...}`` node), and
``thumbnail.originalUrl``. The item's top-level ``id`` (Yahoo's UUID) is the stable
identity the DB cache keys and dedupes on.

This is the only module that knows ``yfinance``/Yahoo exists; swap it and nothing else
changes. It is deliberately defensive — Yahoo is an unofficial, best-effort feed that
reshapes payloads without notice and rate-limits data-centre IPs — so any vendor failure
becomes ``StockDataUnavailable`` and a symbol Yahoo carries no news for yields an empty
run rather than an error. Behind the persistent DB cache, a blocked live call just serves
the stored articles. The fetch is routed through ``yfinance_session`` so a transient crumb
401 — which yfinance can swallow into an empty list — is retried once with a fresh crumb.

An article with no id, no title, or an unparseable publish time is dropped (there'd be no
identity to key on or no time to order by); everything past those three fields is
best-effort and simply left ``None`` when the payload omits it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import yfinance as yf

from app.stocks.adapters import yfinance_session
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.news.entities import NewsArticle, StockNews
from app.stocks.news.ports import NewsProvider


class YfinanceNewsProvider(NewsProvider):
    """Fetches a stock's recent news from Yahoo (no API key)."""

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to
        # the real thing.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_news(self, symbol: str) -> StockNews:
        try:
            # An empty list is how yfinance can surface a swallowed crumb 401, so retry
            # once with a fresh crumb; a genuine no-news symbol just comes back empty
            # after that.
            items = yfinance_session.call(
                lambda: self._ticker_factory(symbol).news,
                is_empty=lambda result: not result,
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(symbol, f"yfinance news failed ({exc})") from exc
        return StockNews(symbol=symbol, articles=tuple(_parse_articles(items)))


def _parse_articles(items) -> list[NewsArticle]:
    """The news list → entities, newest first.

    Rows without an id, a title, or a parseable publish time are dropped, as is a
    duplicate id (first wins). An empty/missing list — how Yahoo presents a symbol with no
    news — yields an empty list, not an error. Keeps all payload-shape handling here."""
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
    """A Yahoo ``pubDate`` (``"2026-07-08T20:04:28Z"``) → a timezone-aware UTC datetime;
    ``None`` for a missing/blank/unparseable value. Accepts an already-parsed datetime
    too (older payloads), normalizing a naive one to UTC."""
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
    """The URL out of a Yahoo link/thumbnail node — a plain string, or a ``{key: url}``
    dict (``canonicalUrl``/``clickThroughUrl`` use ``url``, ``thumbnail`` uses
    ``originalUrl``). ``None`` for anything else or a blank value."""
    if isinstance(node, str):
        return node or None
    if isinstance(node, dict):
        return _clean(node.get(key))
    return None


def _provider_name(content) -> str | None:
    """The outlet's display name from ``content.provider``, or ``None`` when absent."""
    provider = content.get("provider")
    if isinstance(provider, dict):
        return _clean(provider.get("displayName"))
    return None


def _clean(value) -> str | None:
    """A trimmed non-empty string, or ``None`` for a missing/blank/non-string value."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
