from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db_only_insider_transactions_adapter import (
    DbOnlyInsiderTransactionsProvider,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.insider_transactions.db_repository import (
    SqlInsiderTransactionsRepository,
)
from app.stocks.insider_transactions.entities import (
    InsiderActivity,
    InsiderTransaction,
)
from app.stocks.insider_transactions.ports import InsiderTransactionsProvider
from app.stocks.insider_transactions.schemas import (
    InsiderActivityResponse,
    InsiderSummaryResponse,
    InsiderTransactionResponse,
)
from app.stocks.insider_transactions.use_cases import GetInsiderTransactions

router = APIRouter(tags=["insider-transactions"])


def get_insider_transactions_provider(
    db: Session = Depends(get_db),
) -> InsiderTransactionsProvider:
    # DB-only read: serve the stored feed, never fetch live from SEC on a read. The weekly cron is
    # the sole populator, so the endpoint never walks the filings inside a user request.
    return DbOnlyInsiderTransactionsProvider(SqlInsiderTransactionsRepository(db))


def get_insider_transactions_use_case(
    provider: InsiderTransactionsProvider = Depends(get_insider_transactions_provider),
) -> GetInsiderTransactions:
    return GetInsiderTransactions(provider)


def _present_transaction(txn: InsiderTransaction) -> InsiderTransactionResponse:
    return InsiderTransactionResponse(
        filing_date=txn.filing_date,
        transaction_date=txn.transaction_date,
        insider_name=txn.insider_name,
        role=txn.role,
        security_title=txn.security_title,
        transaction_code=txn.transaction_code,
        code_label=txn.code_label,
        acquired_disposed=txn.acquired_disposed,
        is_open_market=txn.is_open_market,
        is_open_market_buy=txn.is_open_market_buy,
        is_open_market_sale=txn.is_open_market_sale,
        shares=txn.shares,
        price_per_share=txn.price_per_share,
        value=txn.value,
        shares_owned_following=txn.shares_owned_following,
    )


def _present(
    activity: InsiderActivity, *, open_market_only: bool
) -> InsiderActivityResponse:
    summary = activity.summary
    txns = activity.open_market if open_market_only else activity.transactions
    return InsiderActivityResponse(
        symbol=activity.symbol,
        count=len(txns),
        summary=InsiderSummaryResponse(
            open_market_buy_count=summary.open_market_buy_count,
            open_market_sell_count=summary.open_market_sell_count,
            open_market_buy_value=summary.open_market_buy_value,
            open_market_sell_value=summary.open_market_sell_value,
            net_value=summary.net_value,
        ),
        transactions=[_present_transaction(t) for t in txns],
    )


@router.get(
    "/stocks/ticker/{ticker}/insider-transactions",
    response_model=InsiderActivityResponse,
)
def get_insider_transactions_endpoint(
    ticker: str,
    response: Response,
    open_market_only: bool = Query(
        False,
        description="Return only the open-market buys and sells (transaction codes P/S), "
        "dropping the grant/exercise/tax/gift transactions a Form 4 also reports.",
    ),
    use_case: GetInsiderTransactions = Depends(get_insider_transactions_use_case),
) -> InsiderActivityResponse:
    try:
        activity = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Insider filings trickle in and the feed is served from the DB cache, so cache briefly: a
    # burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(activity, open_market_only=open_market_only)
