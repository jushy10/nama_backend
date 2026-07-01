"""Interface Adapter: the database-backed AnalystEstimatesRepository.

Forward analyst estimates move slowly (a handful of revisions a quarter) but FMP's
free tier caps at ~250 calls/day, so rather than hit FMP on every snapshot we cache
the consensus in the database — populated lazily on a miss and refreshed out of band
by the estimates cron endpoint (``SyncAnalystEstimates``). The live endpoint reads it
through the ``DbCachedAnalystEstimatesProvider`` decorator.

Two tables, both owned here:

- ``stocks`` — a thin anchor (UUID id, unique ``symbol``, optional company ``name``)
  that per-feature tables hang off of, so the same stock is one row everyone points
  at rather than a symbol string copied around.
- ``stock_analyst_estimates`` — one row per stock holding the current consensus. We
  persist FY1 in full plus the FY2 EPS *and* revenue, which is everything the entity
  needs to serve the forward P/E, forward P/S, and the FY1→FY2 forward growth (the
  later years of FMP's series aren't used downstream, so they aren't stored).

This module owns both ORM models and the repository that maps rows to the
``AnalystEstimates`` *entity*. The domain entity stays free of SQLAlchemy; only this
adapter knows the tables exist.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.stocks.entities import AnalystEstimates, ForwardEstimate
from app.stocks.estimates.estimates_ports import (
    AnalystEstimatesRepository,
    CachedEstimates,
    RefreshTarget,
)


class StockRecord(Base):
    """A stock as stored in the database — the anchor per-feature tables reference.

    ``id`` is a surrogate UUID so child rows have a stable foreign key; ``symbol`` is
    the ticker everything is looked up by (unique); ``name`` is the company display
    name, nullable so a lazily-stored symbol (which arrives with only its ticker)
    still gets a row until a sync fills the name in.
    """

    __tablename__ = "stocks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)


class StockAnalystEstimatesRecord(Base):
    """One stock's current forward consensus — at most one row per stock.

    FY1 (the nearest forward fiscal year) is stored in full; for FY2 (the year after)
    only the EPS and revenue are kept, since downstream the year-2 figures feed only
    the FY1→FY2 growth. ``fetched_at`` stamps the refresh so the cache decorator can
    judge staleness. All estimate columns are nullable: an uncovered symbol still
    gets a (stamped, all-null) row so it isn't re-fetched on every request.
    """

    __tablename__ = "stock_analyst_estimates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # FY1 — the nearest forward fiscal year, in full.
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    eps_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    num_analysts_eps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_analysts_revenue: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # FY2 — only the figures the forward growth needs.
    fiscal_year_fy2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eps_avg_fy2: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_avg_fy2: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _fy2_revenue(estimates: AnalystEstimates) -> float | None:
    """FY2's consensus revenue, pulled from the forward series (the headline fields
    carry FY2 EPS but not revenue). ``None`` when the series doesn't reach FY2."""
    years = estimates.forward_years
    return years[1].revenue_avg if len(years) > 1 else None


def _to_entity(row: StockAnalystEstimatesRecord) -> AnalystEstimates:
    """Rebuild the ``AnalystEstimates`` entity from a stored row.

    The two-row forward series is reconstructed from the FY1/FY2 columns so the
    entity's forward-growth methods work as if it came straight from FMP. The series
    rows need a ``period_end``; FY1's is stored, FY2's is synthesized from its fiscal
    year (the value is never surfaced — only the EPS/revenue are read by the growth
    math).
    """
    forward_years: list[ForwardEstimate] = []
    if row.fiscal_year is not None:
        forward_years.append(
            ForwardEstimate(
                fiscal_year=row.fiscal_year,
                period_end=row.period_end or date(row.fiscal_year, 12, 31),
                eps_avg=row.eps_avg,
                revenue_avg=row.revenue_avg,
            )
        )
    if row.fiscal_year_fy2 is not None:
        forward_years.append(
            ForwardEstimate(
                fiscal_year=row.fiscal_year_fy2,
                period_end=date(row.fiscal_year_fy2, 12, 31),
                eps_avg=row.eps_avg_fy2,
                revenue_avg=row.revenue_avg_fy2,
            )
        )
    return AnalystEstimates(
        fiscal_year=row.fiscal_year,
        period_end=row.period_end,
        eps_avg=row.eps_avg,
        eps_low=row.eps_low,
        eps_high=row.eps_high,
        revenue_avg=row.revenue_avg,
        num_analysts_eps=row.num_analysts_eps,
        num_analysts_revenue=row.num_analysts_revenue,
        eps_avg_fy2=row.eps_avg_fy2,
        fiscal_year_fy2=row.fiscal_year_fy2,
        forward_years=tuple(forward_years),
    )


class SqlAnalystEstimatesRepository(AnalystEstimatesRepository):
    """Reads and writes the analyst-estimates cache through a request-scoped session.

    Holds the session the router injects via ``get_db`` (the same shape as
    ``SqlConstituentRepository``). ``upsert`` commits its own write so a successful
    cache fill is durable independent of the surrounding request.
    """

    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> CachedEstimates | None:
        row = self._session.execute(
            select(StockAnalystEstimatesRecord)
            .join(StockRecord, StockAnalystEstimatesRecord.stock_id == StockRecord.id)
            .where(StockRecord.symbol == symbol)
        ).scalar_one_or_none()
        if row is None:
            return None
        return CachedEstimates(_to_entity(row), row.fetched_at)

    def upsert(
        self, symbol: str, name: str | None, estimates: AnalystEstimates
    ) -> None:
        stock = self._session.execute(
            select(StockRecord).where(StockRecord.symbol == symbol)
        ).scalar_one_or_none()
        if stock is None:
            stock = StockRecord(symbol=symbol, name=name)
            self._session.add(stock)
            self._session.flush()  # assign stock.id before the child row references it
        elif name and not stock.name:
            # Fill a missing name, but never clobber a known one with None.
            stock.name = name

        row = self._session.execute(
            select(StockAnalystEstimatesRecord).where(
                StockAnalystEstimatesRecord.stock_id == stock.id
            )
        ).scalar_one_or_none()
        if row is None:
            row = StockAnalystEstimatesRecord(stock_id=stock.id)
            self._session.add(row)

        row.fiscal_year = estimates.fiscal_year
        row.period_end = estimates.period_end
        row.eps_avg = estimates.eps_avg
        row.eps_low = estimates.eps_low
        row.eps_high = estimates.eps_high
        row.revenue_avg = estimates.revenue_avg
        row.num_analysts_eps = estimates.num_analysts_eps
        row.num_analysts_revenue = estimates.num_analysts_revenue
        row.fiscal_year_fy2 = estimates.fiscal_year_fy2
        row.eps_avg_fy2 = estimates.eps_avg_fy2
        row.revenue_avg_fy2 = _fy2_revenue(estimates)
        row.fetched_at = self._now()
        self._session.commit()

    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        # Oldest-fetched first, so each capped run renews the stalest rows; symbols
        # never viewed (hence never stored) aren't returned — the endpoint fills those
        # lazily on first access. Joined to ``stocks`` to carry the display name along.
        rows = self._session.execute(
            select(StockRecord.symbol, StockRecord.name)
            .join(
                StockAnalystEstimatesRecord,
                StockAnalystEstimatesRecord.stock_id == StockRecord.id,
            )
            .order_by(StockAnalystEstimatesRecord.fetched_at.asc())
            .limit(limit)
        ).all()
        return [RefreshTarget(symbol, name) for symbol, name in rows]
