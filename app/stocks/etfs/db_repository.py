"""Interface Adapters: the SQLAlchemy-backed ETF repositories.

Both implement ``repository.py`` against the slice's own ``etfs`` table and are the only layer
that touches SQLAlchemy:

- ``SqlEtfRepository`` (write side): ``upsert_screen`` writes the screen into ``etfs`` — filling
  ticker/name/exchange fill-once, refreshing the ``net_assets``/``expense_ratio``/``ytd_return``
  figures + the screen stamp on every run. Additive (an absent fund is kept, never deleted);
  commits its own write so a successful sync is durable independent of the request.
- ``SqlEtfSearchRepository`` (read side): the ``GET /stocks/etfs`` search, reading those same
  columns back. Read-only.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, nulls_last, or_, select
from sqlalchemy.orm import Session

from app.stocks.etfs.entities import (
    EtfSearchCriteria,
    EtfSearchPage,
    EtfSearchResult,
    EtfSort,
    ScreenedEtf,
    SortDirection,
)
from app.stocks.etfs.models import EtfRecord, get_or_create_etf
from app.stocks.etfs.repository import (
    EtfRepository,
    EtfSearchRepository,
    EtfSyncCounts,
)


class SqlEtfRepository(EtfRepository):
    """Writes the screened ETF set through a request-scoped session, into the ``etfs`` table.
    ``upsert_screen`` commits its own write so a successful sync is durable independent of the
    surrounding request."""

    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the screen stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def upsert_screen(self, etfs: tuple[ScreenedEtf, ...]) -> EtfSyncCounts:
        now = self._now()
        added = 0
        updated = 0
        for etf in etfs:
            row = get_or_create_etf(self._session, etf.ticker, etf.name)
            # A fund is "added" the first time the screen marks it (screened_at still null);
            # else it's an in-place refresh.
            if row.screened_at is None:
                added += 1
            else:
                updated += 1
            # Fill the identity fact when missing; never clobber a settled value (the name is
            # handled the same way inside get_or_create_etf).
            if etf.exchange and not row.exchange:
                row.exchange = etf.exchange
            # Refresh the drifting screen figures + freshness stamp on every run.
            row.net_assets = etf.net_assets
            row.expense_ratio = etf.expense_ratio
            row.ytd_return = etf.ytd_return
            row.screened_at = now
        self._session.commit()
        return EtfSyncCounts(added=added, updated=updated)


# Each domain sort field → the ``etfs`` column it orders by. All three are nullable, so
# whichever is chosen gets wrapped in nulls_last below — a missing figure sorts to the bottom in
# either direction.
_SORT_COLUMNS = {
    EtfSort.NET_ASSETS: EtfRecord.net_assets,
    EtfSort.YTD_RETURN: EtfRecord.ytd_return,
    EtfSort.EXPENSE_RATIO: EtfRecord.expense_ratio,
}


def _escape_like(term: str) -> str:
    """Escape the LIKE metacharacters in a user's search term so a literal ``%`` / ``_`` matches
    itself instead of acting as a wildcard. Paired with ``escape="\\"`` on the ``.ilike`` calls
    below (backslash is escaped first so it doesn't double-escape the rest)."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _to_result(row: EtfRecord) -> EtfSearchResult:
    """Map an ``etfs`` row onto the slice's read entity (DB facts only, no live price)."""
    return EtfSearchResult(
        ticker=row.ticker,
        name=row.name,
        exchange=row.exchange,
        net_assets=row.net_assets,
        expense_ratio=row.expense_ratio,
        ytd_return=row.ytd_return,
    )


class SqlEtfSearchRepository(EtfSearchRepository):
    """Reads the stored ETF set off the ``etfs`` table through a request-scoped session.
    Read-only — the search never writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search(self, criteria: EtfSearchCriteria) -> EtfSearchPage:
        conditions = self._conditions(criteria)
        # Total match count, before the page window, so the client can render a pager.
        total = self._session.execute(
            select(func.count()).select_from(EtfRecord).where(*conditions)
        ).scalar_one()
        column = _SORT_COLUMNS[criteria.sort]
        ordering = (
            column.desc() if criteria.direction is SortDirection.DESC else column.asc()
        )
        rows = (
            self._session.execute(
                select(EtfRecord)
                .where(*conditions)
                # nulls_last so a fund missing the sort figure sinks to the bottom (either
                # direction); ticker as a stable tiebreak so offset paging over equal values
                # never skips or repeats a row.
                .order_by(nulls_last(ordering), EtfRecord.ticker.asc())
                .limit(criteria.limit)
                .offset(criteria.offset)
            )
            .scalars()
            .all()
        )
        return EtfSearchPage(
            results=tuple(_to_result(row) for row in rows),
            total=total,
            limit=criteria.limit,
            offset=criteria.offset,
        )

    def _conditions(self, criteria: EtfSearchCriteria) -> list:
        """The WHERE terms shared by the count and the page query — just the free-text filter
        when set. There's no 'screened' gate: every row in ``etfs`` came from the screen (unlike
        the stock anchor, which also holds incidentally-known, unscreened tickers)."""
        conditions: list = []
        if criteria.query:
            like = f"%{_escape_like(criteria.query)}%"
            # Match name OR ticker — so "gold" surfaces a gold-miners ETF (by name) and "SPY"
            # matches by ticker.
            conditions.append(
                or_(
                    EtfRecord.name.ilike(like, escape="\\"),
                    EtfRecord.ticker.ilike(like, escape="\\"),
                )
            )
        return conditions
