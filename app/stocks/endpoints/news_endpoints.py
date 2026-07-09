"""HTTP API for reading a stock's recent news.

``GET /stocks/{symbol}/news`` — the read endpoint for the news slice: the stock's latest
headlines, served from the DB cache over yfinance. Controller + presenter + wiring, the
composition-root way, sitting in ``app/stocks/endpoints/`` beside the cron entrypoint
(``cron_news_endpoints``) so all of the slice's HTTP lives in one place.

Wiring convention: the process-singleton live provider is memoized with ``@lru_cache``
while the DB cache is built per request (it needs the request session). A persistent DB
cache (filled lazily on a miss, refreshed out of band by the cron endpoint) sits in front
of Yahoo so the endpoint rarely calls it — Yahoo rate-limits, so the fewer live hits the
better. yfinance needs no credential, so the endpoint is always wired.
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db_cached_news_adapter import DbCachedNewsProvider
from app.stocks.adapters.yfinance_news_adapter import YfinanceNewsProvider
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.news.db_repository import SqlNewsRepository
from app.stocks.news.entities import NewsArticle, StockNews
from app.stocks.news.ports import NewsProvider
from app.stocks.news.schemas import NewsArticleResponse, StockNewsResponse
from app.stocks.news.use_cases import GetStockNews

router = APIRouter(tags=["news"])


@lru_cache(maxsize=1)
def _yfinance_news_provider() -> NewsProvider:
    # One process-singleton live provider (no key, no connection pool to share); the DB
    # cache that wraps it is built per request, since it needs the request session.
    return YfinanceNewsProvider()


def get_news_provider(db: Session = Depends(get_db)) -> NewsProvider:
    # A persistent DB cache (refreshed out of band by the news cron endpoint + lazily on a
    # miss) sits in front of Yahoo so the endpoint rarely calls it, and it serves stored
    # rows without a live round-trip. yfinance needs no key, so this is always wired.
    return DbCachedNewsProvider(_yfinance_news_provider(), SqlNewsRepository(db))


def get_news_use_case(
    provider: NewsProvider = Depends(get_news_provider),
) -> GetStockNews:
    return GetStockNews(provider)


def _present_article(article: NewsArticle) -> NewsArticleResponse:
    return NewsArticleResponse(
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


def _present(news: StockNews) -> StockNewsResponse:
    """Presenter: stock-news entity -> HTTP response DTO."""
    latest = news.latest
    return StockNewsResponse(
        symbol=news.symbol,
        count=len(news.articles),
        latest=_present_article(latest) if latest else None,
        articles=[_present_article(a) for a in news.articles],
    )


@router.get("/stocks/{symbol}/news", response_model=StockNewsResponse)
def get_stock_news_endpoint(
    symbol: str,
    response: Response,
    use_case: GetStockNews = Depends(get_news_use_case),
) -> StockNewsResponse:
    try:
        news = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # News is served from the DB cache and refreshed out of band, so cache briefly: a
    # burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(news)
