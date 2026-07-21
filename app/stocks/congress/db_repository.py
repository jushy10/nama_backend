from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.congress import models
from app.stocks.congress.entities import CongressActivity, CongressTrade
from app.stocks.congress.models import StockCongressTradeRecord
from app.stocks.congress.repository import CongressTradesRepository, RefreshTarget

# How many trades to keep per stock. A member-disclosure feed turns over faster than the annual
# earnings/segments series, so this bounds the higher-volume history — pruned by row (like the news
# / insider feeds), not by fiscal period.
_MAX_STORED_TRADES = 100


def _to_entity(
    row: StockCongressTradeRecord,
    *,
    ticker: str,
    company_name: str | None,
) -> CongressTrade:
    return CongressTrade(
        member=row.member,
        chamber=row.chamber,
        party=row.party,
        ticker=ticker,
        company_name=company_name,
        tx_type=row.tx_type,
        amount_range=row.amount_range,
        transaction_date=row.transaction_date,
        disclosure_date=row.disclosure_date,
        owner=row.owner,
        source_url=row.source_url,
    )


class SqlCongressTradesRepository(CongressTradesRepository):
    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> CongressActivity | None:
        rows = models.trades_by_symbol(self._session, symbol)
        if not rows:
            return None
        # A per-ticker read: the ticker is the requested symbol and the company name is the anchor
        # name (read once off the parent row — the same for every trade of this stock).
        stock = self._session.get(models.StockRecord, rows[0].stock_id)
        company_name = stock.name if stock is not None else None
        return CongressActivity(
            symbol=symbol,
            trades=tuple(
                _to_entity(row, ticker=symbol, company_name=company_name) for row in rows
            ),
        )

    def recent_market_activity(
        self, *, since: date | None, limit: int, offset: int
    ) -> tuple[list[CongressTrade], int]:
        rows = models.recent_market_trades(
            self._session, since=since, limit=limit, offset=offset
        )
        trades = [
            _to_entity(row[0], ticker=row.ticker, company_name=row.name) for row in rows
        ]
        total = models.count_recent_market_trades(self._session, since=since)
        return trades, total

    def market_trades_in_window(self, *, since: date | None) -> list[CongressTrade]:
        rows = models.market_trades_in_window(self._session, since=since)
        return [
            _to_entity(row[0], ticker=row.ticker, company_name=row.name) for row in rows
        ]

    def upsert(self, symbol: str, name: str | None, activity: CongressActivity) -> None:
        stock = models.get_or_create_stock(self._session, symbol, name)

        # Insert-only: a filed disclosure never changes, so add only the ones we don't already have
        # and never rewrite an existing row. Diff the fresh set against the stored keys, and also
        # de-dup within this batch (a member can appear once per identity key per fetch).
        existing = models.existing_keys_for_stock(self._session, stock.id)
        now = self._now()
        for trade in activity.trades:
            key = (
                trade.member,
                trade.transaction_date,
                trade.amount_range,
                trade.chamber,
            )
            if key in existing:
                continue
            existing.add(key)  # guard against duplicate rows within a single fetch
            self._session.add(
                StockCongressTradeRecord(
                    stock_id=stock.id,
                    member=trade.member,
                    chamber=trade.chamber,
                    party=trade.party,
                    tx_type=trade.tx_type,
                    amount_range=trade.amount_range,
                    transaction_date=trade.transaction_date,
                    disclosure_date=trade.disclosure_date,
                    owner=trade.owner,
                    source_url=trade.source_url,
                    fetched_at=now,
                )
            )
        # Flush the pending inserts before the prune. The request session (``get_db`` /
        # ``SessionLocal``) is ``autoflush=False``, so without this the prune's SELECT would not see
        # the just-added rows and the newest-N cap would be computed over the wrong (smaller) set —
        # silently over-storing on the first fetch of a heavily-traded stock. (A raw test
        # ``Session`` defaults to autoflush=True, which is why this only bites in production.)
        self._session.flush()
        # Refresh the as-of stamp across the stock's whole feed so a quiet stock (confirmed with no
        # new activity) still reads as fresh to the sweep's staleness order. New rows already carry
        # ``now``.
        models.touch_fetched_at(self._session, stock.id, now)
        # Cap the accumulated feed so it stays bounded. Prune after the insert so the just-added
        # trades are in the running when the newest N are chosen.
        models.prune_to_newest(self._session, stock.id, _MAX_STORED_TRADES)
        self._session.commit()

    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        # Delegates the query to models (un-cached first, then least-recently-refreshed); this layer
        # just wraps each (symbol, name) pair in the domain-facing RefreshTarget.
        return [
            RefreshTarget(symbol, name)
            for symbol, name in models.stalest_symbols(self._session, limit)
        ]
