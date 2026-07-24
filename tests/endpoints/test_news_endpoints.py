from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.endpoints import news_endpoints as endpoints
from app.endpoints.error_handlers import register_error_handlers
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.coverage.news.entities import NewsArticle, StockNews


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def run(self, symbol: str) -> StockNews:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    register_error_handlers(app)  # the endpoint has no try/except; the handlers translate
    # Overriding the shim replaces the whole construction chain (db session included).
    app.dependency_overrides[endpoints.get_get_stock_news] = lambda: fake
    return TestClient(app)


def _article(article_id, *, published, title="Headline", **kw) -> NewsArticle:
    return NewsArticle(id=article_id, title=title, published_at=published, **kw)


def test_presents_the_run_with_fields_and_latest():
    news = StockNews(
        "AAPL",
        (
            _article(
                "a2",
                published=datetime(2026, 7, 8, 20, 4, 28, tzinfo=timezone.utc),
                title="Newer",
                publisher="Reuters",
                link="https://example.com/2",
                content_type="VIDEO",
            ),
            _article("a1", published=datetime(2026, 7, 1, tzinfo=timezone.utc), title="Older"),
        ),
    )
    fake = _FakeUseCase(result=news)
    resp = _client(fake).get("/stocks/AAPL/news")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "AAPL"
    assert body["count"] == 2
    assert body["latest"]["id"] == "a2"
    assert body["latest"]["title"] == "Newer"
    assert body["latest"]["publisher"] == "Reuters"
    assert body["latest"]["link"] == "https://example.com/2"
    assert body["latest"]["is_video"] is True
    assert len(body["articles"]) == 2
    assert body["articles"][1]["is_video"] is False
    assert fake.calls == ["AAPL"]


def test_sets_the_cache_header():
    fake = _FakeUseCase(
        result=StockNews(
            "AAPL", (_article("a1", published=datetime(2026, 7, 8, tzinfo=timezone.utc)),)
        )
    )
    resp = _client(fake).get("/stocks/AAPL/news")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_empty_coverage_is_a_200_with_no_articles():
    fake = _FakeUseCase(result=StockNews("ZZZZ", ()))
    resp = _client(fake).get("/stocks/ZZZZ/news")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 0
    assert body["latest"] is None
    assert body["articles"] == []


def test_bad_symbol_is_a_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _client(fake).get("/stocks/123/news").status_code == 400


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ZZZZ/news").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("AAPL", "boom"))
    assert _client(fake).get("/stocks/AAPL/news").status_code == 502
