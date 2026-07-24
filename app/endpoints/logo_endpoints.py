import os

from fastapi import APIRouter, Depends, HTTPException, Response

from app.domains.profile.logo import wiring
from app.domains.profile.logo.use_cases import GetStockLogo

router = APIRouter(tags=["stocks"])


def get_get_stock_logo() -> GetStockLogo:
    # Shim over the framework-free wiring: env config + the missing-token 503 gate
    # stay at this edge (Logo.dev needs a free *publishable* token, logo.dev,
    # 500k/mo; without it the logo endpoint returns 503, mirroring how the Alpaca
    # keys gate price data), and dependency_overrides gets its test seam.
    token = os.environ.get("LOGODEV_TOKEN")
    if not token:
        raise HTTPException(503, "Logos are not configured (LOGODEV_TOKEN).")
    provider = wiring.get_logo_provider(token, os.environ.get("LOGODEV_BASE_URL"))
    return wiring.build_get_stock_logo(provider)


@router.get(
    "/stocks/{symbol}/logo",
    responses={200: {"content": {"image/png": {}}}},
    response_class=Response,
)
def get_stock_logo_image(
    symbol: str, use_case: GetStockLogo = Depends(get_get_stock_logo)
) -> Response:
    try:
        logo = use_case.run(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Domain errors (StockNotFound -> 404, StockDataUnavailable -> 502) are translated
    # by the central handlers in endpoints/error_handlers.py.
    return Response(content=logo.content, media_type=logo.media_type)
