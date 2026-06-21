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
from app.stocks.entities import Stock
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import StockDataProvider
from app.stocks.schemas import StockResponse
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


def get_stock_info(provider: StockDataProvider = Depends(get_provider)) -> GetStockInfo:
    return GetStockInfo(provider)


def get_stock_logo(provider: StockDataProvider = Depends(get_provider)) -> GetStockLogo:
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
        image = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return Response(content=image, media_type="image/png")
