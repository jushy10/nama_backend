"""HTTP API for reading a stock's forward PEG.

``GET /stocks/ticker/{symbol}`` — the read endpoint for the ticker slice: the forward
PEG (forward P/E over expected FY1→FY2 EPS growth), the one valuation figure no other
endpoint serves. Its legs deliberately stay snapshot-only (``forward_pe`` and
``growth.forward_eps_growth`` on ``GET /stocks/{symbol}``) so the same numbers don't
get two homes that could disagree. Controller + presenter + wiring, the
composition-root way, sitting in ``app/stocks/endpoints/`` like the other slices' HTTP.

Wiring convention: this endpoint owns no vendor of its own — it reuses the composition
root's factories. The price rides the ``@lru_cache``d Alpaca provider (and inherits its
503 gate when the keys are missing: the price is primary here), and the estimates ride
the annual-earnings projection (DB-only, no key). There's no cron or table behind this
endpoint: the PEG embeds the live price, so it's computed per request — freshness of the
consensus legs is the annual-earnings slice's job (lazy fill + its sync cron).
"""

from fastapi import APIRouter, Depends, HTTPException, Response

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import AnalystEstimatesProvider, StockQuoteProvider
from app.stocks.router import get_estimates_provider, get_provider
from app.stocks.ticker.entities import TickerValuation
from app.stocks.ticker.schemas import TickerValuationResponse
from app.stocks.ticker.use_cases import GetTickerValuation

router = APIRouter(tags=["ticker"])


def get_ticker_valuation_use_case(
    quotes: StockQuoteProvider = Depends(get_provider),
    estimates: AnalystEstimatesProvider = Depends(get_estimates_provider),
) -> GetTickerValuation:
    # Same Alpaca instance as the snapshot/quote endpoints (get_quote is the slim
    # snapshot half) + the same DB-only estimates projection the snapshot's forward
    # P/E uses — one source of truth for both the price and the consensus.
    return GetTickerValuation(quotes, estimates)


def _present(valuation: TickerValuation) -> TickerValuationResponse:
    """Presenter: ticker-valuation entity -> HTTP response DTO.

    The entity speaks in the domain term ``symbol``; renaming it ``ticker`` (and
    serving only the PEG + the price it embeds) is a JSON-shape choice made here
    at the edge, like the DTOs' other shape concerns."""
    return TickerValuationResponse(
        ticker=valuation.symbol,
        price=valuation.price,
        forward_peg=valuation.forward_peg,
    )


@router.get("/stocks/ticker/{symbol}", response_model=TickerValuationResponse)
def get_ticker_valuation_endpoint(
    symbol: str,
    response: Response,
    use_case: GetTickerValuation = Depends(get_ticker_valuation_use_case),
) -> TickerValuationResponse:
    try:
        valuation = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # A valuation card, not a ticking price: the consensus legs move on analyst
    # revisions and the multiple doesn't need tick precision, so cache briefly —
    # a burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(valuation)
