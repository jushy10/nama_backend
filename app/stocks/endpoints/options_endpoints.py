"""HTTP API for the options-flow resource — one stock's live chain and the flow over it.

``GET /stocks/ticker/{ticker}/options`` — the calls and puts coming in for a stock: one
expiration's full chain (strike ladder, volume, open interest, implied volatility, and
the dollar premium into each contract), the day's aggregate flow (per-side volume/OI, the
put/call lean, net premium), and the "unusual activity" standouts (contracts trading
above their open interest — fresh positioning), most-money-first. ``?expiration=`` selects
an expiry (default: the nearest upcoming), and the response lists every expiry so a client
can switch without a second call.

Where the ticker card's ``options_metrics`` block distils the chain into four summary
reads, this serves the chain itself — the deeper "options-flow" view. Live per request
(an options chain decays by the hour, so there's no table/cron behind it — the same stance
as the card's options block); the chain is keyless via yfinance and is this endpoint's
reason to exist, so a vendor block is a 502 while a symbol that simply lists no options is
a 200 with an empty flow.

Controller + presenter + wiring, the composition-root way, sitting in
``app/stocks/endpoints/`` like the other slices' HTTP. It owns no vendor of its own beyond
the keyless yfinance chain — no shared singleton to reuse — so the wiring is a small local
factory.
"""

from datetime import date
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.stocks.adapters.yfinance_options_flow_adapter import (
    YfinanceOptionsChainProvider,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.options.entities import ExpiryChain, OptionContract, OptionsFlowSummary
from app.stocks.options.ports import OptionsChainProvider
from app.stocks.options.schemas import (
    OptionContractResponse,
    OptionsFlowResponse,
    OptionsFlowSummaryResponse,
)
from app.stocks.options.use_cases import GetOptionsFlow, OptionsFlow

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
    """Presenter: options-flow composition → HTTP response DTO. The domain speaks in
    ``symbol``; renaming it ``ticker`` is a JSON-shape choice made here at the edge. A
    symbol with no listed options carries a ``None`` chain — served as null expiry/summary
    and empty lists rather than a 404."""
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
    """A stock's options-flow read for one expiration: the full calls/puts chain, the
    day's aggregate flow, and the unusual-activity standouts. Keyless (Yahoo via
    yfinance) and computed live — Yahoo publishes cumulative day volume and prior-day open
    interest, not a trade-by-trade tape, so this is a "where's the volume and money going"
    snapshot, not a print-level flow feed. A blocked/failed fetch is a 502; a symbol with
    no listed options is a 200 with an empty flow."""
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
