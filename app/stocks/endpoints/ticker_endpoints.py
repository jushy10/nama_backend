"""HTTP API for reading a stock's ticker card.

``GET /stocks/ticker/{ticker}`` — the read endpoint for the ticker slice: the live
quote (price + day move), the clean company name, the listing exchange (served from
the ``stocks`` row, learned once from the price feed), and market cap, plus **opt-in
blocks** requested via ``?include=`` — ``dividend``, ``performance`` (trailing
windows), and ``metrics`` (trailing PEG + margins off the fundamentals call, and the
**forward PEG**: forward P/E over expected FY1→FY2 EPS growth, the one valuation
figure no other endpoint serves). Pay-per-use: a block that isn't requested costs no
provider call. The forward PEG's legs (forward P/E, forward EPS growth) are
deliberately not serialized here — they stay on the shared entities, feeding the
AI analysis context — so the same numbers don't get two homes that could
disagree. Controller + presenter +
wiring, the composition-root way, sitting in ``app/stocks/endpoints/`` like the
other slices' HTTP.

Wiring convention: this endpoint owns no vendor of its own — it reuses the composition
root's factories. The quote and performance windows ride the ``@lru_cache``d Alpaca
provider (whose missing-keys 503 gate the endpoint inherits: the quote is primary
here), the name and fundamentals ride the optional Finnhub providers (best-effort,
``None`` without a key), and the estimates ride the annual-earnings projection
(DB-only, no key). There's no cron or table behind this endpoint: the card is built
around the live quote, so it's computed per request — freshness of the consensus legs
is the annual-earnings slice's job (lazy fill + its sync cron).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.entities import StockPerformance
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import (
    AnalystEstimatesProvider,
    CompanyProfileProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
)
from app.stocks.router import (
    get_estimates_provider,
    get_fundamentals_provider,
    get_profile_provider,
    get_provider,
)
from app.stocks.schemas import StockPerformanceResponse
from app.stocks.ticker.db_repository import SqlTickerRepository
from app.stocks.ticker.schemas import (
    DividendResponse,
    TickerCardResponse,
    TickerMetricsResponse,
)
from app.stocks.ticker.use_cases import GetTickerCard, TickerCard

router = APIRouter(tags=["ticker"])


def get_ticker_card_use_case(
    provider=Depends(get_provider),
    estimates: AnalystEstimatesProvider = Depends(get_estimates_provider),
    fundamentals: StockFundamentalsProvider | None = Depends(get_fundamentals_provider),
    profile: CompanyProfileProvider | None = Depends(get_profile_provider),
    db: Session = Depends(get_db),
) -> GetTickerCard:
    # The Alpaca singleton backs the quote, the trailing performance windows, and the
    # one-time exchange lookup (same instance as the snapshot/quote endpoints), and
    # the estimates are the same DB-only projection the snapshot's forward P/E uses —
    # one source of truth for every leg the card carries. The profile provider
    # supplies the display name (the slim quote carries none), TTL-cached like on the
    # snapshot; the repository serves the stored exchange off the stocks row.
    performance = provider if isinstance(provider, StockPerformanceProvider) else None
    return GetTickerCard(
        provider,
        estimates,
        fundamentals,
        performance,
        profile,
        stocks=provider,
        repository=SqlTickerRepository(db),
    )


def _round2(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


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


def _present(card: TickerCard) -> TickerCardResponse:
    """Presenter: ticker-card composition -> HTTP response DTO.

    The domain speaks in ``symbol``; renaming it ``ticker`` is a JSON-shape choice
    made here at the edge, like the DTOs' other shape concerns. Opt-in blocks are
    emitted only when the card was asked to carry them — ``card.include`` gates
    the dividend block and the metrics' trailing half, since both ride the
    fundamentals the market cap needs anyway; performance is already ``None``
    when unrequested."""
    fundamentals = card.fundamentals
    dividend = None
    if "dividend" in card.include and fundamentals is not None:
        # Rounded here at the edge: a dividend card shows cents / basis-point-ish
        # precision, and the vendor's raw figures carry float noise. The shared
        # entity stays unrounded — the snapshot serves the same fields raw.
        dividend = DividendResponse(
            yield_percentage=_round2(fundamentals.dividend_yield),
            per_share=_round2(fundamentals.dividend_per_share),
        )
    metrics = None
    if "metrics" in card.include:
        # Trailing ratios ride the fundamentals the market cap already fetched;
        # the forward PEG comes off the valuation built from the stored consensus.
        trailing = fundamentals.metrics if fundamentals else None
        metrics = TickerMetricsResponse(
            peg=trailing.peg if trailing else None,
            forward_peg=card.valuation.forward_peg if card.valuation else None,
            gross_margin=trailing.gross_margin if trailing else None,
            operating_margin=trailing.operating_margin if trailing else None,
            net_margin=trailing.net_margin if trailing else None,
        )
    return TickerCardResponse(
        ticker=card.quote.symbol,
        name=card.name,
        exchange=card.exchange,
        price=card.quote.price,
        change=card.quote.change,
        change_percent=card.quote.change_percent,
        market_cap=fundamentals.market_cap if fundamentals else None,
        dividend=dividend,
        performance=_present_performance(card.performance),
        metrics=metrics,
    )


@router.get("/stocks/ticker/{ticker}", response_model=TickerCardResponse)
def get_ticker_card_endpoint(
    ticker: str,
    response: Response,
    include: list[str] | None = Query(
        default=None,
        description=(
            "Opt-in blocks to include: dividend, performance, metrics. Repeat the "
            "param or comma-separate (?include=dividend,metrics). Unrequested "
            "blocks are null and cost no upstream call."
        ),
    ),
    use_case: GetTickerCard = Depends(get_ticker_card_use_case),
) -> TickerCardResponse:
    try:
        card = use_case.execute(ticker, include=include)
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
    return _present(card)
