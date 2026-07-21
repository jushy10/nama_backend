from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.institutional_ownership import models
from app.stocks.institutional_ownership.entities import (
    HOLDER_TYPE_INSTITUTION,
    HOLDER_TYPE_MUTUAL_FUND,
    InstitutionalHolder,
    InstitutionalOwnership,
    OwnershipBreakdown,
)
from app.stocks.institutional_ownership.models import (
    StockInstitutionalHolderRecord,
    StockOwnershipSummaryRecord,
)
from app.stocks.institutional_ownership.repository import (
    InstitutionalOwnershipRepository,
    RefreshTarget,
)

# How many holder rows to keep per stock. The feed is a multi-quarter history of two ~top-10 lists
# (institutions + funds) per reported quarter, so this bounds it to roughly the newest three
# quarters — plenty of history without unbounded growth (pruned by row, like the news feed).
_MAX_STORED_HOLDERS = 60

# The holder types the merge recognises when grouping snapshots — defensively confined to the two
# the adapter emits, so an unexpected value can't silently expand the stored vocabulary.
_KNOWN_HOLDER_TYPES = frozenset({HOLDER_TYPE_INSTITUTION, HOLDER_TYPE_MUTUAL_FUND})


def _to_holder(row: StockInstitutionalHolderRecord) -> InstitutionalHolder:
    return InstitutionalHolder(
        holder=row.holder,
        holder_type=row.holder_type,
        date_reported=row.date_reported,
        shares=row.shares,
        value=row.value,
        pct_held=row.pct_held,
        pct_change=row.pct_change,
    )


def _to_breakdown(
    row: StockOwnershipSummaryRecord | None,
) -> OwnershipBreakdown | None:
    if row is None:
        return None
    breakdown = OwnershipBreakdown(
        institutions_pct_held=row.institutions_pct_held,
        insiders_pct_held=row.insiders_pct_held,
        institutions_float_pct_held=row.institutions_float_pct_held,
        institutions_count=row.institutions_count,
    )
    return None if breakdown.is_empty else breakdown


class SqlInstitutionalOwnershipRepository(InstitutionalOwnershipRepository):
    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> InstitutionalOwnership | None:
        rows = models.holders_by_symbol(self._session, symbol)
        if not rows:
            return None  # a stored symbol always has ≥1 holder row → None means "never cached"
        breakdown = _to_breakdown(models.summary_by_symbol(self._session, symbol))
        return InstitutionalOwnership(
            symbol=symbol,
            breakdown=breakdown,
            holders=tuple(_to_holder(row) for row in rows),
        )

    def upsert(
        self, symbol: str, name: str | None, ownership: InstitutionalOwnership
    ) -> None:
        stock = models.get_or_create_stock(self._session, symbol, name)
        now = self._now()

        # Merge the holders feed: replace only the (holder_type, reported quarter) snapshots this
        # fetch re-served, then insert the fresh rows — earlier reported quarters are left intact,
        # so the store accumulates a history the source never serves at once.
        holders = [
            h for h in ownership.holders if h.holder_type in _KNOWN_HOLDER_TYPES
        ]
        snapshots = {(h.holder_type, h.date_reported) for h in holders}
        models.delete_holder_snapshots(self._session, stock.id, snapshots)
        for holder in holders:
            self._session.add(
                StockInstitutionalHolderRecord(
                    stock_id=stock.id,
                    holder=holder.holder,
                    holder_type=holder.holder_type,
                    date_reported=holder.date_reported,
                    shares=holder.shares,
                    value=holder.value,
                    pct_held=holder.pct_held,
                    pct_change=holder.pct_change,
                    fetched_at=now,
                )
            )
        # Flush the pending inserts before the prune. The request session (``get_db`` /
        # ``SessionLocal``) is ``autoflush=False``, so without this the prune's SELECT would not see
        # the just-added rows and the newest-N cap would be computed over the wrong set — silently
        # over-storing. (A raw test ``Session`` defaults to autoflush=True, which is why this only
        # bites in production.)
        self._session.flush()
        models.prune_to_newest(self._session, stock.id, _MAX_STORED_HOLDERS)

        # Overwrite the single ownership-breakdown row (Yahoo publishes only a current snapshot).
        self._upsert_summary(stock.id, ownership.breakdown, now)

        self._session.commit()

    def _upsert_summary(self, stock_id, breakdown: OwnershipBreakdown | None, now) -> None:
        row = models.summary_for_stock(self._session, stock_id)
        if row is None:
            row = StockOwnershipSummaryRecord(stock_id=stock_id)
            self._session.add(row)
        row.institutions_pct_held = (
            breakdown.institutions_pct_held if breakdown else None
        )
        row.insiders_pct_held = breakdown.insiders_pct_held if breakdown else None
        row.institutions_float_pct_held = (
            breakdown.institutions_float_pct_held if breakdown else None
        )
        row.institutions_count = breakdown.institutions_count if breakdown else None
        row.fetched_at = now

    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        return [
            RefreshTarget(symbol, name)
            for symbol, name in models.stalest_symbols(self._session, limit)
        ]
