from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.domains.pricing.options import wiring
from app.domains.pricing.options.api_schemas import OptionsFlowResponse
from app.domains.pricing.options.use_cases import GetOptionsFlow

router = APIRouter(tags=["options"])


def get_get_options_flow() -> GetOptionsFlow:
    # Depends shim over the slice's wiring — exists for the dependency_overrides
    # test seam, nothing more (the yfinance source is keyless, so no 503 gate).
    return wiring.build_get_options_flow()


@router.get("/stocks/ticker/{ticker}/options", response_model=OptionsFlowResponse)
def get_options_flow_endpoint(
    ticker: str,
    response: Response,
    expiration: date | None = Query(
        default=None,
        description=(
            "The option expiration to show (YYYY-MM-DD). Must be one the symbol lists "
            "(see the returned `expirations`). Omit for the nearest upcoming expiry."
        ),
    ),
    use_case: GetOptionsFlow = Depends(get_get_options_flow),
) -> OptionsFlowResponse:
    try:
        # Bad request input (invalid symbol / unlisted expiration) surfaces as a ValueError
        # from the use case — an inline 400, deliberately kept in the endpoint. Domain errors
        # (StockNotFound → 404, StockDataUnavailable → 502) are translated by the central
        # handlers in endpoints/error_handlers.py.
        flow = use_case.run(ticker, expiration=expiration)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Options data moves intraday (volume accrues, quotes tick), so cache only briefly —
    # enough to collapse a burst of viewers onto one fetch without going stale.
    response.headers["Cache-Control"] = "public, max-age=120"
    return OptionsFlowResponse.from_flow(flow)
