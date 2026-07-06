"""Database model + queries for the ETF slice.

The ``etfs`` table this feature owns, plus a simple, entity-free helper over it. Unlike the
earnings tables — child time-series hanging off the shared ``stocks`` anchor — this is a
**standalone anchor**: an ETF is not a company, so it lives in its own table rather than as a
``stocks`` row (which would leak funds into the stock universe search). ``get_or_create_etf``
mirrors ``get_or_create_stock``'s fill-but-don't-clobber contract. The concrete repository
(``db_repository.py``) is the only caller; it maps these rows to and from the ``ScreenedEtf``
entity, so this layer deals only in rows and columns.

The schema is created by migration 0016.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, String, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


class EtfRecord(Base):
    """A US ETF as stored — one row per fund, the screened top-ETF set.

    ``id`` is a surrogate UUID; ``ticker`` is what everything is looked up by (unique). ``name``
    and ``exchange`` are fill-once identity facts (nullable until the first screen that carries
    them). ``net_assets`` (AUM, whole dollars), ``expense_ratio`` and ``ytd_return`` (percents)
    are the screen figures — a moving snapshot refreshed on every run; all nullable, since a
    given screen may omit a figure. ``screened_at`` is when the last screen that included the
    fund ran (the freshness stamp).
    """

    __tablename__ = "etfs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticker: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    net_assets: Mapped[float | None] = mapped_column(Float, nullable=True)
    expense_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    ytd_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    screened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


def get_or_create_etf(session: Session, ticker: str, name: str | None) -> EtfRecord:
    """Return the ``etfs`` row for ``ticker``, creating it if absent.

    Fills a missing name when one is supplied, but never clobbers a known name with ``None`` —
    the same fill-once contract ``get_or_create_stock`` applies. The new row is flushed so its
    ``id`` is available within the same unit of work.
    """
    etf = session.execute(
        select(EtfRecord).where(EtfRecord.ticker == ticker)
    ).scalar_one_or_none()
    if etf is None:
        etf = EtfRecord(ticker=ticker, name=name)
        session.add(etf)
        session.flush()
    elif name and not etf.name:
        etf.name = name
    return etf
