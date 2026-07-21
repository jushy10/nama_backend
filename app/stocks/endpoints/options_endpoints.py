from datetime import date
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.stocks.adapters.yfinance.options_flow_adapter import (
    YfinanceOptionsChainProvider,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.company.options.entities import ExpiryChain, OptionContract, OptionsFlowSummary
from app.stocks.company.options.ports import OptionsChainProvider
from app.stocks.company.options.schemas import (
    OptionContractResponse,
    OptionsFlowResponse,
    OptionsFlowSummaryResponse,
)
from app.stocks.company.options.use_cases import GetOptionsFlow, OptionsFlow

router = APIRouter(tags=["options"])

# The unusual-activity list is a "look here first" highlight, not the whole chain, so it's
# capped — the full picture is already in `calls`/`puts`. Most-money-first (the entity's
# ordering), so the cap keeps the biggest bets.
_MAX_UNUSUAL = 25


@lru_cache(maxsize=1)
def get_options_chain_provider() -> OptionsChainProvider:
    # Keyless yfinance singleton (like the ticker card's options provider): best-effort at
    # read and always constructable, so there's no key gate. A blocked Yahoo call surfaces
    # as a 502 at the endpoint (the chain is primary here), not a boot-time failure.
    return YfinanceOptionsChainProvider()


def get_options_flow_use_case(
    options: OptionsChainProvider = Depends(get_options_chain_provider),
) -> GetOptionsFlow:
    return GetOptionsFlow(options)


def _round2(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _present_contract(c: OptionContract) -> OptionContractResponse:
    # Display figures rounded here at the edge — the chain arithmetic (mid, premium, the
    # ratio) carries float noise — and IV rendered as a percent (the entity keeps the
    # vendor's decimal fraction).
    iv = None if c.implied_volatility is None else round(c.implied_volatility * 100, 2)
    return OptionContractResponse(
        expiration=c.expiration,
        strike=c.strike,
        type=c.option_type.value,
        bid=c.bid,
        ask=c.ask,
        last_price=c.last_price,
        mid=_round2(c.mid),
        volume=c.volume,
        open_interest=c.open_interest,
        implied_volatility=iv,
        in_the_money=c.in_the_money,
        premium=_round2(c.premium),
        volume_oi_ratio=_round2(c.volume_oi_ratio),
        unusual=c.is_unusual,
    )


def _present_summary(s: OptionsFlowSummary) -> OptionsFlowSummaryResponse:
    return OptionsFlowSummaryResponse(
        call_volume=s.call_volume,
        put_volume=s.put_volume,
        total_volume=s.total_volume,
        call_open_interest=s.call_open_interest,
        put_open_interest=s.put_open_interest,
        put_call_volume_ratio=_round2(s.put_call_volume_ratio),
        put_call_oi_ratio=_round2(s.put_call_oi_ratio),
        call_premium=_round2(s.call_premium),
        put_premium=_round2(s.put_premium),
        net_premium=_round2(s.net_premium),
    )


def _present(flow: OptionsFlow) -> OptionsFlowResponse:
    chain: ExpiryChain | None = flow.chain
    if chain is None:
        return OptionsFlowResponse(
            ticker=flow.symbol,
            expirations=list(flow.expirations),
        )
    return OptionsFlowResponse(
        ticker=flow.symbol,
        spot=_round2(chain.spot),
        expiration=chain.expiration,
        expirations=list(flow.expirations),
        summary=_present_summary(chain.summary),
        calls=[_present_contract(c) for c in chain.calls],
        puts=[_present_contract(c) for c in chain.puts],
        unusual=[_present_contract(c) for c in chain.unusual[:_MAX_UNUSUAL]],
    )


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
    use_case: GetOptionsFlow = Depends(get_options_flow_use_case),
) -> OptionsFlowResponse:
    try:
        flow = use_case.execute(ticker, expiration=expiration)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Options data moves intraday (volume accrues, quotes tick), so cache only briefly —
    # enough to collapse a burst of viewers onto one fetch without going stale.
    response.headers["Cache-Control"] = "public, max-age=120"
    return _present(flow)
