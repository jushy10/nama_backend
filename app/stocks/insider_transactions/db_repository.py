from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.insider_transactions import models
from app.stocks.insider_transactions.entities import (
    InsiderActivity,
    InsiderTransaction,
)
from app.stocks.insider_transactions.models import StockInsiderTransactionRecord
from app.stocks.insider_transactions.repository import (
    InsiderTransactionsRepository,
    RefreshTarget,
)

# How many transactions to keep per stock. A Form 4 feed turns over faster than the annual
# earnings/segments series, so this bounds the higher-volume history — pruned by row (like the
# news feed), not by fiscal year.
_MAX_STORED_TRANSACTIONS = 100


def _to_entity(row: StockInsiderTransactionRecord) -> InsiderTransaction:
    return InsiderTransaction(
        filing_date=row.filing_date,
        transaction_date=row.transaction_date,
        insider_name=row.insider_name,
        officer_title=row.officer_title,
        is_director=row.is_director,
        is_officer=row.is_officer,
        is_ten_percent_owner=row.is_ten_percent_owner,
        security_title=row.security_title,
        transaction_code=row.transaction_code,
        acquired_disposed=row.acquired_disposed,
        shares=row.shares,
        price_per_share=row.price_per_share,
        shares_owned_following=row.shares_owned_following,
        accession_number=row.accession_number,
        line_index=row.line_index,
    )


class SqlInsiderTransactionsRepository(InsiderTransactionsRepository):
    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> InsiderActivity | None:
        rows = models.transactions_by_symbol(self._session, symbol)
        if not rows:
            return None
        return InsiderActivity(
            symbol=symbol, transactions=tuple(_to_entity(row) for row in rows)
        )

    def upsert(self, symbol: str, name: str | None, activity: InsiderActivity) -> None:
        stock = models.get_or_create_stock(self._session, symbol, name)

        # Insert-only: a filed transaction never changes, so add only the ones we don't already
        # have and never rewrite an existing row. Diff the fresh set against the stored keys.
        existing = models.existing_keys_for_stock(self._session, stock.id)
        now = self._now()
        for txn in activity.transactions:
            if (txn.accession_number, txn.line_index) in existing:
                continue
            self._session.add(
                StockInsiderTransactionRecord(
                    stock_id=stock.id,
                    filing_date=txn.filing_date,
                    transaction_date=txn.transaction_date,
                    insider_name=txn.insider_name,
                    officer_title=txn.officer_title,
                    is_director=txn.is_director,
                    is_officer=txn.is_officer,
                    is_ten_percent_owner=txn.is_ten_percent_owner,
                    security_title=txn.security_title,
                    transaction_code=txn.transaction_code,
                    acquired_disposed=txn.acquired_disposed,
                    shares=txn.shares,
                    price_per_share=txn.price_per_share,
                    shares_owned_following=txn.shares_owned_following,
                    accession_number=txn.accession_number,
                    line_index=txn.line_index,
                    fetched_at=now,
                )
            )
        # Flush the pending inserts before the prune. The request session (``get_db`` /
        # ``SessionLocal``) is ``autoflush=False``, so without this the prune's SELECT would not
        # see the just-added rows and the newest-N cap would be computed over the wrong (smaller)
        # set — silently over-storing on the first fetch of a heavy filer. (A raw test
        # ``Session`` defaults to autoflush=True, which is why this only bites in production.)
        self._session.flush()
        # Refresh the as-of stamp across the stock's whole feed so a quiet stock (confirmed with
        # no new activity) still reads as fresh to the TTL cache — otherwise a repeat read past
        # the TTL would re-fetch on every request. New rows already carry ``now``.
        models.touch_fetched_at(self._session, stock.id, now)
        # Cap the accumulated feed so it stays bounded. Prune after the insert so the just-added
        # transactions are in the running when the newest N are chosen.
        models.prune_to_newest(self._session, stock.id, _MAX_STORED_TRANSACTIONS)
        self._session.commit()

    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        # Delegates the query to models (un-cached first, then least-recently-refreshed); this
        # layer just wraps each (symbol, name) pair in the domain-facing RefreshTarget.
        return [
            RefreshTarget(symbol, name)
            for symbol, name in models.stalest_symbols(self._session, limit)
        ]
