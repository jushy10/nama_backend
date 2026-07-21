from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.stocks.entities import StockPerformance
from app.stocks.catalog.performance.interfaces import PerformanceRepositoryAdapter
from app.stocks.catalog.anchor.models import StockRecord


class PerformanceRepositoryAdapterImpl(PerformanceRepositoryAdapter):
    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the sync stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def refresh_targets(self, limit: int | None) -> tuple[str, ...]:
        # Screened rows only (market_cap IS NOT NULL) — the universe the heat map colours and
        # the search ranks; an incidentally-known ticker isn't worth a bars fetch. Un-synced
        # first (NULL performance_synced_at sorts ahead), then stalest, via the same portable
        # NULLS-first ordering the fundamentals sweep uses. `None` limit returns every screened
        # ticker so one sweep can seed them all (the batched feed makes that cheap).
        synced = StockRecord.performance_synced_at
        stmt = (
            select(StockRecord.ticker)
            .where(StockRecord.market_cap.is_not(None))
            .order_by(synced.is_(None).desc(), synced.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return tuple(self._session.execute(stmt).scalars().all())

    def set_performance(
        self, performance_by_ticker: Mapping[str, StockPerformance]
    ) -> int:
        # Overwrite the six windows + stamp for every ticker the batched feed returned, in one
        # commit (the sweep values the whole batch, so per-ticker commits would be needless
        # churn). A ticker with no anchor row is skipped. Only written rows are stamped, so a
        # target the feed didn't return stays un-stamped and leads the stale queue next run.
        now = self._now()
        written = 0
        for ticker, performance in performance_by_ticker.items():
            stock = self._session.execute(
                select(StockRecord).where(StockRecord.ticker == ticker)
            ).scalar_one_or_none()
            if stock is None:
                continue
            stock.perf_one_week = performance.one_week
            stock.perf_one_month = performance.one_month
            stock.perf_three_month = performance.three_month
            stock.perf_six_month = performance.six_month
            stock.perf_ytd = performance.ytd
            stock.perf_one_year = performance.one_year
            stock.performance_synced_at = now
            written += 1
        self._session.commit()
        return written
