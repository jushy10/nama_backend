"""Interface Adapter: the database-backed CompanyProfileRepository.

A company's display name and business description barely change, but fetching them
means calling Finnhub (name) and FMP (description) — the latter against a ~250/day
free quota. So we cache the merged profile in the database, filled lazily on a miss
and refreshed by ``scripts/sync_profiles.py``; the live endpoint reads it through the
``DbCachedCompanyProfileProvider`` decorator.

The profile spans two tables: the **name** lives on the shared ``stocks`` anchor
(``stock_record.py``) — it's the company name every feature wants — and only the
**description** gets a table of its own here. So ``get`` joins the two and ``upsert``
writes the name onto ``stocks`` (via ``get_or_create_stock``) and the description onto
``stock_company_profile``. This module owns the description table + the repository
that maps the pair to the ``CompanyProfile`` *entity*; the entity stays ORM-free.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Text, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.stocks.entities import CompanyProfile
from app.stocks.ports import CachedProfile, CompanyProfileRepository
from app.stocks.stock_record import StockRecord, get_or_create_stock


class StockCompanyProfileRecord(Base):
    """One stock's cached business description — at most one row per stock.

    Only the description lives here; the company name rides the shared ``stocks``
    row. ``fetched_at`` stamps the refresh so the cache decorator can judge staleness.
    """

    __tablename__ = "stock_company_profile"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlCompanyProfileRepository(CompanyProfileRepository):
    """Reads and writes the profile cache through a request-scoped session.

    The name is read from / written to the shared ``stocks`` anchor and the
    description from / to ``stock_company_profile``, so a profile is the join of the
    two. ``upsert`` commits its own write, like the estimates repository.
    """

    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> CachedProfile | None:
        row = self._session.execute(
            select(StockCompanyProfileRecord, StockRecord.name)
            .join(StockRecord, StockCompanyProfileRecord.stock_id == StockRecord.id)
            .where(StockRecord.symbol == symbol)
        ).first()
        if row is None:
            return None
        profile_row, name = row
        profile = CompanyProfile(name=name, description=profile_row.description)
        return CachedProfile(profile, profile_row.fetched_at)

    def upsert(self, symbol: str, profile: CompanyProfile) -> None:
        # The profile's name is the canonical company name — store it on the anchor
        # (filling a missing one, never clobbering a known one).
        stock = get_or_create_stock(self._session, symbol, profile.name)

        row = self._session.execute(
            select(StockCompanyProfileRecord).where(
                StockCompanyProfileRecord.stock_id == stock.id
            )
        ).scalar_one_or_none()
        if row is None:
            row = StockCompanyProfileRecord(stock_id=stock.id)
            self._session.add(row)

        row.description = profile.description
        row.fetched_at = self._now()
        self._session.commit()
