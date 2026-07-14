"""HTTP API for reading Congressional stock trades — per-ticker and market-wide.

Two reads:

- ``GET /stocks/ticker/{ticker}/congress-trades`` — a stock's recent Congressional trades, newest
  first, with a net buy-vs-sell ``summary``. Grouped under the ``/stocks/ticker/{ticker}`` resource
  (like the ticker card, analyst-info, and insider-transactions), since it's a per-ticker card the
  FE renders.
- ``GET /market/congress-activity`` — a window of the *whole market's* recent Congressional trades,
  newest first (the market board). Grouped under ``/market/`` beside the heat map.

Both reads are **DB-only**: they serve the stored feed straight from the database and never fetch
the multi-megabyte source on a read. Keeping the store current is entirely the weekly
``sync-congress`` cron's job. Best-effort throughout — a stock (or a window) with no activity is a
``200`` with an empty ``items`` list, never a 404. No credential is needed, so the endpoints are
always wired.

Controller + presenter + wiring, the composition-root way, sitting in ``app/stocks/endpoints/``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.congress.db_repository import SqlCongressTradesRepository
from app.stocks.congress.entities import (
    CongressMarketActivity,
    CongressSummary,
    CongressTrade,
)
from app.stocks.congress.schemas import (
    CongressActivityResponse,
    CongressMarketActivityResponse,
    CongressSummaryResponse,
    CongressTradeResponse,
)
from app.stocks.congress.use_cases import (
    GetCongressActivity,
    GetCongressTrades,
    parse_window,
)

router = APIRouter(tags=["congress"])


def get_congress_trades_use_case(db: Session = Depends(get_db)) -> GetCongressTrades:
    # DB-only read: serve the stored feed, never fetch the source on a read. The weekly cron is the
    # sole populator, so the endpoint never downloads the feed inside a user request.
    return GetCongressTrades(SqlCongressTradesRepository(db))


def get_congress_activity_use_case(db: Session = Depends(get_db)) -> GetCongressActivity:
    return GetCongressActivity(SqlCongressTradesRepository(db))


# --- Presenters --------------------------------------------------------------------------


def _present_trade(trade: CongressTrade) -> CongressTradeResponse:
    return CongressTradeResponse(
        member=trade.member,
        chamber=trade.chamber,
        party=trade.party,
        ticker=trade.ticker,
        name=trade.company_name,
        tx_type=trade.tx_type,
        amount_range=trade.amount_range,
        amount_midpoint=trade.amount_midpoint,
        transaction_date=trade.transaction_date,
        disclosure_date=trade.disclosure_date,
        owner=trade.owner,
        source_url=trade.source_url,
        is_buy=trade.is_buy,
        is_sell=trade.is_sell,
    )


def _present_summary(summary: CongressSummary) -> CongressSummaryResponse:
    return CongressSummaryResponse(
        buy_count=summary.buy_count,
        sell_count=summary.sell_count,
        buy_value=summary.buy_value,
        sell_value=summary.sell_value,
        net_value=summary.net_value,
    )


# --- Per-ticker read ---------------------------------------------------------------------


@router.get(
    "/stocks/ticker/{ticker}/congress-trades",
    response_model=CongressActivityResponse,
)
def get_congress_trades_endpoint(
    ticker: str,
    response: Response,
    limit: int = Query(
        50, ge=1, le=200, description="Max trades to return in this page."
    ),
    offset: int = Query(0, ge=0, description="How many trades to skip (pagination)."),
    use_case: GetCongressTrades = Depends(get_congress_trades_use_case),
) -> CongressActivityResponse:
    try:
        activity = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    all_trades = activity.trades
    page = all_trades[offset : offset + limit]
    # Slow-moving DB feed refreshed out of band by the cron, so cache briefly: a burst of viewers
    # collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return CongressActivityResponse(
        symbol=activity.symbol,
        total=len(all_trades),
        limit=limit,
        offset=offset,
        count=len(page),
        # The summary always reflects the full stored set, not just the page.
        summary=_present_summary(activity.summary),
        items=[_present_trade(t) for t in page],
    )


# --- Market-wide board -------------------------------------------------------------------


@router.get(
    "/market/congress-activity",
    response_model=CongressMarketActivityResponse,
)
def get_congress_activity_endpoint(
    response: Response,
    window: str = Query(
        "30d",
        description="Time window over the disclosure date: 7d, 30d, 90d, 180d, 1y or all.",
    ),
    limit: int = Query(
        50, ge=1, le=200, description="Max trades to return in this page."
    ),
    offset: int = Query(0, ge=0, description="How many trades to skip (pagination)."),
    use_case: GetCongressActivity = Depends(get_congress_activity_use_case),
) -> CongressMarketActivityResponse:
    try:
        window_days = parse_window(window)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    activity: CongressMarketActivity = use_case.execute(
        window_days=window_days, limit=limit, offset=offset
    )
    response.headers["Cache-Control"] = "public, max-age=300"
    return CongressMarketActivityResponse(
        window=window.strip().lower(),
        total=activity.total,
        limit=limit,
        offset=offset,
        count=len(activity.trades),
        summary=_present_summary(activity.summary),
        items=[_present_trade(t) for t in activity.trades],
    )
