"""Controller + Presenter + dependency wiring for the stocks feature.

The controller (`get_stock`) adapts an HTTP request into a use-case call; the
presenter (`_present`) adapts the returned Stock entity into the HTTP DTO.

Credentials are read from the environment (like DATABASE_URL in app/db.py).
The provider is built lazily so the app still boots without Alpaca keys —
the error only surfaces when the endpoint is actually called.
"""

import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response

from app.stocks.alpaca_provider import AlpacaStockDataProvider
from app.stocks.entities import Stock, StockPerformance
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.finnhub_fundamentals_provider import FinnhubFundamentalsProvider
from app.stocks.fmp_logo_provider import FmpLogoProvider
from app.stocks.ports import (
    LogoProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
)
from app.stocks.schemas import StockPerformanceResponse, StockResponse
from app.stocks.use_cases import GetStockInfo, GetStockLogo

router = APIRouter(tags=["stocks"])


@lru_cache(maxsize=1)
def get_provider() -> StockDataProvider:
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise HTTPException(
            503, "Stock data is not configured (APCA_API_KEY_ID / APCA_API_SECRET_KEY)."
        )
    return AlpacaStockDataProvider(key, secret)


@lru_cache(maxsize=1)
def get_fundamentals_provider() -> StockFundamentalsProvider | None:
    # Best-effort enrichment: without a key we simply omit market cap + dividend
    # (price + performance still serve). Free key from finnhub.io.
    key = os.environ.get("FINNHUB_API_KEY")
    return FinnhubFundamentalsProvider(key) if key else None


def get_stock_info(
    provider: StockDataProvider = Depends(get_provider),
    fundamentals: StockFundamentalsProvider | None = Depends(get_fundamentals_provider),
) -> GetStockInfo:
    # The Alpaca provider supplies both the snapshot and the performance windows.
    performance = provider if isinstance(provider, StockPerformanceProvider) else None
    return GetStockInfo(provider, performance, fundamentals)


@lru_cache(maxsize=1)
def get_logo_provider() -> LogoProvider:
    # No credentials needed; the source is free. LOGO_BASE_URL lets you point
    # at a different ticker-keyed source without a code change.
    base_url = os.environ.get("LOGO_BASE_URL")
    return FmpLogoProvider(base_url) if base_url else FmpLogoProvider()


def get_stock_logo(provider: LogoProvider = Depends(get_logo_provider)) -> GetStockLogo:
    return GetStockLogo(provider)


def _present(stock: Stock) -> StockResponse:
    """Presenter: domain entity -> HTTP response DTO."""
    return StockResponse(
        symbol=stock.symbol,
        name=stock.name,
        exchange=stock.exchange,
        price=stock.price,
        change=stock.change,
        change_percent=stock.change_percent,
        open=stock.open,
        high=stock.high,
        low=stock.low,
        previous_close=stock.previous_close,
        volume=stock.volume,
        bid=stock.bid,
        ask=stock.ask,
        spread=stock.spread,
        as_of=stock.as_of,
        market_cap=stock.market_cap,
        dividend_per_share=stock.dividend_per_share,
        dividend_yield=stock.dividend_yield,
        performance=_present_performance(stock.performance),
    )


def _present_performance(
    perf: StockPerformance | None,
) -> StockPerformanceResponse | None:
    if perf is None:
        return None
    return StockPerformanceResponse(
        one_week=perf.one_week,
        one_month=perf.one_month,
        three_month=perf.three_month,
        six_month=perf.six_month,
        ytd=perf.ytd,
        one_year=perf.one_year,
    )


@router.get("/stocks/{symbol}", response_model=StockResponse)
def get_stock(
    symbol: str, use_case: GetStockInfo = Depends(get_stock_info)
) -> StockResponse:
    try:
        stock = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present(stock)


@router.get(
    "/stocks/{symbol}/logo",
    responses={200: {"content": {"image/png": {}}}},
    response_class=Response,
)
def get_stock_logo_image(
    symbol: str, use_case: GetStockLogo = Depends(get_stock_logo)
) -> Response:
    try:
        logo = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return Response(content=logo.content, media_type=logo.media_type)
