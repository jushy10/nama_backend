import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response

from app.stocks.adapters.logodev_adapter import LogoDevProvider
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.logo.ports import LogoProvider
from app.stocks.logo.use_cases import GetStockLogo

router = APIRouter(tags=["stocks"])


@lru_cache(maxsize=1)
def get_logo_provider() -> LogoProvider:
    # Logo.dev keeps logos current through rebrands/symbol changes. It needs a
    # free *publishable* token (logo.dev, 500k/mo); without it the logo endpoint
    # returns 503, mirroring how the Alpaca keys gate price data. LOGODEV_BASE_URL
    # lets tests point elsewhere without a code change.
    token = os.environ.get("LOGODEV_TOKEN")
    if not token:
        raise HTTPException(503, "Logos are not configured (LOGODEV_TOKEN).")
    base_url = os.environ.get("LOGODEV_BASE_URL")
    return LogoDevProvider(token, base_url) if base_url else LogoDevProvider(token)


def get_stock_logo(provider: LogoProvider = Depends(get_logo_provider)) -> GetStockLogo:
    return GetStockLogo(provider)


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
