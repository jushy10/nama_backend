from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.domains.ownership.congress.congress_trades_repository_adapter_impl import CongressTradesRepositoryAdapterImpl
from app.domains.ownership.congress.entities import (
    CongressLeaderboard,
    CongressLeaderboardEntry,
    CongressMarketActivity,
    CongressSummary,
    CongressTrade,
)
from app.domains.ownership.congress.schemas import (
    CongressActivityResponse,
    CongressLeaderboardEntryResponse,
    CongressLeaderboardResponse,
    CongressMarketActivityResponse,
    CongressSummaryResponse,
    CongressTradeResponse,
)
from app.domains.ownership.congress.use_cases import (
    GetCongressActivity,
    GetCongressLeaderboard,
    GetCongressTrades,
    parse_metric,
    parse_window,
)

router = APIRouter(tags=["congress"])


def get_congress_trades_use_case(db: Session = Depends(get_db)) -> GetCongressTrades:
    # DB-only read: serve the stored feed, never fetch the source on a read. The weekly cron is the
    # sole populator, so the endpoint never downloads the feed inside a user request.
    return GetCongressTrades(CongressTradesRepositoryAdapterImpl(db))


def get_congress_activity_use_case(db: Session = Depends(get_db)) -> GetCongressActivity:
    return GetCongressActivity(CongressTradesRepositoryAdapterImpl(db))


def get_congress_leaderboard_use_case(
    db: Session = Depends(get_db),
) -> GetCongressLeaderboard:
    return GetCongressLeaderboard(CongressTradesRepositoryAdapterImpl(db))


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


def _present_leaderboard_entry(
    entry: CongressLeaderboardEntry,
) -> CongressLeaderboardEntryResponse:
    return CongressLeaderboardEntryResponse(
        ticker=entry.ticker,
        name=entry.company_name,
        trade_count=entry.trade_count,
        member_count=entry.member_count,
        buy_count=entry.buy_count,
        sell_count=entry.sell_count,
        buy_value=entry.buy_value,
        sell_value=entry.sell_value,
        net_value=entry.net_value,
        total_value=entry.total_value,
        last_activity=entry.last_activity,
    )


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


@router.get(
    "/market/congress-leaderboard",
    response_model=CongressLeaderboardResponse,
)
def get_congress_leaderboard_endpoint(
    response: Response,
    window: str = Query(
        "30d",
        description="Time window over the disclosure date: 7d, 30d, 90d, 180d, 1y or all.",
    ),
    metric: str = Query(
        "members",
        description=(
            "How to rank: members (distinct members trading it), trades (disclosure count) "
            "or value (estimated gross dollars moved)."
        ),
    ),
    limit: int = Query(
        20, ge=1, le=100, description="Max stocks to return in the ranked board."
    ),
    use_case: GetCongressLeaderboard = Depends(get_congress_leaderboard_use_case),
) -> CongressLeaderboardResponse:
    try:
        window_days = parse_window(window)
        metric_key = parse_metric(metric)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    board: CongressLeaderboard = use_case.execute(
        window_days=window_days, metric=metric_key, limit=limit
    )
    response.headers["Cache-Control"] = "public, max-age=300"
    return CongressLeaderboardResponse(
        window=window.strip().lower(),
        metric=board.metric,
        total=board.total_stocks,
        count=len(board.entries),
        items=[_present_leaderboard_entry(e) for e in board.entries],
    )
