from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.company.revenue_segments import models
from app.stocks.company.revenue_segments.entities import (
    RevenueSegment,
    RevenueSegmentation,
    SegmentAxis,
)
from app.stocks.company.revenue_segments.models import StockRevenueSegmentRecord
from app.stocks.company.revenue_segments.interfaces import (
    RefreshTarget,
    RevenueSegmentsRepositoryAdapter,
)

# How many fiscal years of disaggregation to keep per stock. A filing restates ~3 years, so a
# handful of years accumulates a useful trend without unbounded growth. Bounded like the news
# feed, but by *year* rather than row count (a year can carry many axis/member rows).
_MAX_STORED_YEARS = 6


def _to_entity(row: StockRevenueSegmentRecord) -> RevenueSegment:
    return RevenueSegment(
        fiscal_year=row.fiscal_year,
        period_end=row.period_end,
        axis=SegmentAxis(row.axis),
        member=row.member,
        value=row.value,
    )


def _to_segmentation(
    symbol: str, rows: list[StockRevenueSegmentRecord]
) -> RevenueSegmentation:
    return RevenueSegmentation(
        symbol=symbol, segments=tuple(_to_entity(row) for row in rows)
    )


class RevenueSegmentsRepositoryAdapterImpl(RevenueSegmentsRepositoryAdapter):
    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> RevenueSegmentation | None:
        rows = models.segments_by_symbol(self._session, symbol)
        if not rows:
            return None
        return _to_segmentation(symbol, rows)

    def upsert(
        self, symbol: str, name: str | None, segmentation: RevenueSegmentation
    ) -> None:
        stock = models.get_or_create_stock(self._session, symbol, name)

        # Merge, don't rewrite: clear only the fiscal years this filing restated, then insert the
        # fresh rows. A reported year's disaggregation is a frozen fact and a filing restates only
        # its most-recent ~3 years, so earlier stored years stay — the history accumulates beyond
        # any single filing's window.
        fresh_years = {seg.fiscal_year for seg in segmentation.segments}
        models.delete_years_for_stock(self._session, stock.id, fresh_years)
        now = self._now()
        for seg in segmentation.segments:
            self._session.add(
                StockRevenueSegmentRecord(
                    stock_id=stock.id,
                    fiscal_year=seg.fiscal_year,
                    period_end=seg.period_end,
                    axis=seg.axis.value,
                    member=seg.member,
                    value=seg.value,
                    fetched_at=now,
                )
            )
        # Cap the accumulated history so it stays bounded. Prune after the insert so the
        # just-fetched years are in the running when the newest N are chosen.
        models.prune_to_newest_years(self._session, stock.id, _MAX_STORED_YEARS)
        self._session.commit()

    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        # Delegates the query to models (un-cached first, then least-recently-refreshed); this
        # layer just wraps each (symbol, name) pair in the domain-facing RefreshTarget.
        return [
            RefreshTarget(symbol, name)
            for symbol, name in models.stalest_symbols(self._session, limit)
        ]
