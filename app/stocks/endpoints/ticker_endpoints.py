"""HTTP API for reading a stock's ticker card.

``GET /stocks/ticker/{ticker}`` — the read endpoint for the ticker slice: the live
quote (price + day move), the clean company name, and the anchor facts served straight
from the ``stocks`` row — the listing exchange (learned once from the price feed) plus
the universe screen's market cap, sector and industry — then **opt-in blocks**
requested via ``?include=`` — ``dividend``, ``performance`` (trailing windows),
``metrics`` (the trailing P/E — price over the quarterly slice's stored TTM EPS, on
the analyst-consensus basis so it pairs with the forward legs — trailing PEG + margins
off the fundamentals call, the **forward PEG**: forward P/E over expected FY1→FY2 EPS
growth, the one valuation figure no other endpoint serves, and the annual slice's
latest trailing YoY revenue/EPS growth off the same anchor read), and
``options_metrics`` (the **options-market read**: ATM implied volatility, the priced-in
expected move, the cost of a protective put, and the day's put/call lean — what the
options market believes about the stock, for a buyer sizing an entry). Pay-per-use: a
block that isn't requested costs no provider call — and market cap now riding the
anchor, the fundamentals call is itself opt-in (only ``dividend``/``metrics`` pull it). The forward PEG's legs (forward P/E, forward
EPS growth) are deliberately not serialized here — they stay on the shared
entities, feeding the AI analysis context — so the same numbers don't get two
homes that could disagree. Controller + presenter + wiring, the
composition-root way, sitting in ``app/stocks/endpoints/`` like the other
slices' HTTP.

Wiring convention: this endpoint owns no vendor of its own — it reuses the composition
root's factories. The quote and performance windows ride the ``@lru_cache``d Alpaca
provider (whose missing-keys 503 gate the endpoint inherits: the quote is primary
here), the name and fundamentals ride the optional Finnhub providers (best-effort,
``None`` without a key), the estimates ride the annual-earnings projection
(DB-only, no key), the trailing P/E's TTM sum rides the quarterly-earnings
slice's read-through DB cache (keyless; live to Yahoo only on a cold miss,
best-effort), and the options chain rides Yahoo via yfinance (keyless,
best-effort even when requested — Yahoo intermittently blocks data-centre IPs, and
a missing insight must not take the quote down). There's no cron or table behind
this endpoint: the card is built around the live quote, so it's computed per
request — freshness of the consensus legs is the annual-earnings slice's job
(lazy fill + its sync cron).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.endpoints.quarterly_earnings_endpoints import (
    get_quarterly_earnings_provider,
)
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
    get_options_provider,
    get_profile_provider,
    get_provider,
)
from app.stocks.schemas import StockPerformanceResponse
from app.stocks.ticker.db_repository import SqlTickerRepository
from app.stocks.ticker.entities import TickerOptionsMetrics
from app.stocks.ticker.ports import OptionChainProvider
from app.stocks.ticker.schemas import (
    DividendResponse,
    OptionsMetricsResponse,
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
    options: OptionChainProvider = Depends(get_options_provider),
    earnings: QuarterlyEarningsProvider = Depends(get_quarterly_earnings_provider),
    db: Session = Depends(get_db),
) -> GetTickerCard:
    # The Alpaca singleton backs the quote, the trailing performance windows, and the
    # one-time exchange lookup (same instance as the snapshot/quote endpoints), and
    # the estimates are the same DB-only projection the snapshot's forward P/E uses —
    # one source of truth for every leg the card carries. The profile provider
    # supplies the display name (the slim quote carries none), TTL-cached like on the
    # snapshot; the repository serves the stored exchange off the stocks row. The
    # options chain is the keyless yfinance singleton — always wired, best-effort
    # at read — and the quarterly-earnings provider is the same DB cache the
    # earnings endpoint reads (lazy-filled on a miss, refreshed by its cron), so
    # the trailing P/E's TTM sum rides rows the earnings view already keeps warm.
    performance = provider if isinstance(provider, StockPerformanceProvider) else None
    return GetTickerCard(
        provider,
        estimates,
        fundamentals,
        performance,
        profile,
        stocks=provider,
        repository=SqlTickerRepository(db),
        options=options,
        earnings=earnings,
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


def _present_options_metrics(
    metrics: TickerOptionsMetrics | None,
) -> OptionsMetricsResponse | None:
    if metrics is None:
        return None
    # Rounded here at the edge like the dividend: these are display figures
    # (percents, a ratio) and the chain arithmetic carries float noise.
    return OptionsMetricsResponse(
        implied_volatility=_round2(metrics.implied_volatility),
        expected_move_percent=_round2(metrics.expected_move_percent),
        expected_move_by=metrics.expected_move_by,
        insurance_cost_percent=_round2(metrics.insurance_cost_percent),
        insurance_expires=metrics.insurance_expires,
        put_call_ratio=_round2(metrics.put_call_ratio),
    )


def _present(card: TickerCard) -> TickerCardResponse:
    """Presenter: ticker-card composition -> HTTP response DTO.

    The domain speaks in ``symbol``; renaming it ``ticker`` is a JSON-shape choice
    made here at the edge, like the DTOs' other shape concerns. Opt-in blocks are
    emitted only when the card was asked to carry them — ``card.include`` gates the
    dividend block and the metrics' fundamentals-backed half (which is ``None`` when
    neither was requested, since fundamentals is only fetched for those); performance
    is already ``None`` when unrequested. Market cap, sector and industry ride the
    anchor read, so they're always served (``null`` until the row carries them)."""
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
        # The P/E pair rides the valuation: trailing off the quarterly slice's
        # TTM sum, forward PEG off the stored consensus — one (adjusted) EPS
        # basis for both, deliberately NOT the vendor's GAAP-ish TTM read. The
        # PEG and margins still ride the fundamentals the market cap fetched.
        trailing = fundamentals.metrics if fundamentals else None
        metrics = TickerMetricsResponse(
            pe=card.valuation.trailing_pe if card.valuation else None,
            peg=trailing.peg if trailing else None,
            forward_peg=card.valuation.forward_peg if card.valuation else None,
            gross_margin=trailing.gross_margin if trailing else None,
            operating_margin=trailing.operating_margin if trailing else None,
            net_margin=trailing.net_margin if trailing else None,
            # The trailing YoY pair rides the anchor read (already rounded percent),
            # not the fundamentals call — so it serves even when Finnhub is down.
            revenue_growth_yoy=card.revenue_growth_yoy,
            eps_growth_yoy=card.eps_growth_yoy,
        )
    return TickerCardResponse(
        ticker=card.quote.symbol,
        name=card.name,
        exchange=card.exchange,
        price=card.quote.price,
        change=card.quote.change,
        change_percent=card.quote.change_percent,
        market_cap=card.market_cap,
        sector=card.sector,
        industry=card.industry,
        dividend=dividend,
        performance=_present_performance(card.performance),
        metrics=metrics,
        options_metrics=_present_options_metrics(card.options_metrics),
    )


@router.get("/stocks/ticker/{ticker}", response_model=TickerCardResponse)
def get_ticker_card_endpoint(
    ticker: str,
    response: Response,
    include: list[str] | None = Query(
        default=None,
        description=(
            "Opt-in blocks to include: dividend, performance, metrics, "
            "options_metrics. Repeat the param or comma-separate "
            "(?include=dividend,metrics). Unrequested blocks are null and cost "
            "no upstream call."
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
