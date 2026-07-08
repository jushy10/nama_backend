"""Database models + queries for the ETF slice.

The ``etfs`` table this feature owns and its two child tables, plus simple, entity-free helpers
over them. Unlike the earnings tables — child time-series hanging off the shared ``stocks``
anchor — ``etfs`` is a **standalone anchor**: an ETF is not a company, so it lives in its own
table rather than as a ``stocks`` row (which would leak funds into the stock universe search).
``get_or_create_etf`` mirrors ``get_or_create_stock``'s fill-but-don't-clobber contract.

``etfs`` carries three groups of columns:
- identity (fill-once): ``ticker`` / ``name`` / ``exchange``;
- the screen figures (refreshed every screen run): ``net_assets`` / ``expense_ratio`` /
  ``screened_at``;
- the per-fund *profile* the enrichment pass fills (``category`` + ``fund_family`` /
  ``dividend_yield`` / ``description`` / ``nav`` / the trailing-return ladder), stamped by
  ``profile_fetched_at``.

The profile's list-valued halves live in their own child tables — ``etf_sector_weightings`` and
``etf_top_holdings`` — each a delete-then-insert-per-fund set hanging off ``etfs`` with ON DELETE
CASCADE. The concrete repository (``db_repository.py``) is the only caller; it maps these rows to
and from the slice entities, so this layer deals only in rows and columns.

The schema is created by migrations 0016 (``etfs``) and 0020 (the profile columns + child tables).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    delete,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


class EtfRecord(Base):
    """A US ETF as stored — one row per fund, the screened top-ETF set.

    ``id`` is a surrogate UUID; ``ticker`` is what everything is looked up by (unique). ``name``
    and ``exchange`` are fill-once identity facts (nullable until the first screen that carries
    them). ``net_assets`` (AUM, whole dollars) and ``expense_ratio`` (percent) are the screen
    figures — a moving snapshot refreshed on every screen run; both nullable, since a given screen
    may omit a figure. ``screened_at`` is when the last screen that included the fund ran.

    The rest is the per-fund **profile** the enrichment pass fills from Yahoo's per-ticker surfaces
    (the screen carries none of it): ``category`` is the classification slug (e.g. ``large_growth``);
    ``fund_family`` / ``description`` are near-static facts; ``dividend_yield`` / ``nav`` and the
    trailing-return ladder (``ytd_return`` / ``three_year_return`` / ``five_year_return``, all
    percents) drift, so the enrichment pass refreshes them. All are percents except ``nav`` (a raw
    per-share price). ``profile_fetched_at`` stamps the last successful profile refresh (null until
    the enrichment pass first reaches the fund) and orders the stalest-first refresh queue. The
    profile's list halves (sector weightings, top holdings) live in the child tables below.
    """

    __tablename__ = "etfs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticker: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    net_assets: Mapped[float | None] = mapped_column(Float, nullable=True)
    expense_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    screened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # --- The per-fund profile (filled by the enrichment pass; see the class docstring) ---
    fund_family: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dividend_yield: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    nav: Mapped[float | None] = mapped_column(Float, nullable=True)
    ytd_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_year_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    five_year_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    profile_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EtfSectorWeightingRecord(Base):
    """One sector's weight in one fund — a child of the ``etfs`` anchor.

    Many rows per fund (one per sector), unique on ``(etf_id, sector)``. ``sector`` is the vendor's
    sector key (a slug, e.g. ``technology``); ``weight`` is a percent of the fund. The enrichment
    pass rewrites a fund's whole set at once (delete-then-insert), so every row for a fund shares
    one ``fetched_at``.
    """

    __tablename__ = "etf_sector_weightings"
    __table_args__ = (
        UniqueConstraint(
            "etf_id", "sector", name="uq_etf_sector_weightings_etf_sector"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    etf_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("etfs.id", ondelete="CASCADE"), nullable=False
    )
    sector: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EtfTopHoldingRecord(Base):
    """One of a fund's largest positions — a child of the ``etfs`` anchor.

    Many rows per fund, unique on ``(etf_id, position)`` — ``position`` is the largest-first rank
    (0-based), the stable key here since a holding's ``ticker`` can be absent for an odd row.
    ``ticker`` / ``name`` identify the holding and ``weight`` is its percent of the fund (all
    nullable — a row contributes whatever it carries). Rewritten per-fund (delete-then-insert), so
    every row shares one ``fetched_at``.
    """

    __tablename__ = "etf_top_holdings"
    __table_args__ = (
        UniqueConstraint(
            "etf_id", "position", name="uq_etf_top_holdings_etf_position"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    etf_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("etfs.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


def profile_refresh_targets(
    session: Session, limit: int | None = None
) -> list[str]:
    """The tickers whose profile most needs a refresh, stalest first — the enrichment pass's
    work-list.

    Every screened fund is a target (the profile figures drift, so there's no "done" state, unlike
    the old fill-once category pass). Ordered **never-fetched first** (a NULL ``profile_fetched_at``
    sorts ahead of any stamped fund), then oldest-refresh first, with ``ticker`` as a stable
    tiebreak — so a capped, rate-limited run spends its budget on the funds most out of date and
    successive capped runs round-robin the whole set rather than starving the tail. ``limit`` caps
    the batch; ``None`` (the default) returns every fund, so one uncapped run refreshes all of them.
    """
    stmt = (
        select(EtfRecord.ticker)
        .order_by(
            EtfRecord.profile_fetched_at.is_(None).desc(),
            EtfRecord.profile_fetched_at.asc(),
            EtfRecord.ticker.asc(),
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars().all())


def sector_weightings_for_etf(
    session: Session, ticker: str
) -> list[EtfSectorWeightingRecord]:
    """A fund's stored sector weightings (joined through the ``etfs`` anchor), weight descending.
    Empty when nothing is stored for it yet."""
    return list(
        session.execute(
            select(EtfSectorWeightingRecord)
            .join(EtfRecord, EtfSectorWeightingRecord.etf_id == EtfRecord.id)
            .where(EtfRecord.ticker == ticker)
            .order_by(EtfSectorWeightingRecord.weight.desc())
        ).scalars()
    )


def top_holdings_for_etf(
    session: Session, ticker: str
) -> list[EtfTopHoldingRecord]:
    """A fund's stored top holdings (joined through the ``etfs`` anchor), largest first by stored
    ``position``. Empty when nothing is stored for it yet."""
    return list(
        session.execute(
            select(EtfTopHoldingRecord)
            .join(EtfRecord, EtfTopHoldingRecord.etf_id == EtfRecord.id)
            .where(EtfRecord.ticker == ticker)
            .order_by(EtfTopHoldingRecord.position.asc())
        ).scalars()
    )


def delete_sector_weightings_for_etf(session: Session, etf_id: uuid.UUID) -> None:
    """Remove a fund's stored sector weightings, so a refresh can rewrite the set wholesale
    (delete-then-insert) rather than diffing rows."""
    session.execute(
        delete(EtfSectorWeightingRecord).where(
            EtfSectorWeightingRecord.etf_id == etf_id
        )
    )


def delete_top_holdings_for_etf(session: Session, etf_id: uuid.UUID) -> None:
    """Remove a fund's stored top holdings, so a refresh can rewrite the set wholesale
    (delete-then-insert) rather than diffing rows."""
    session.execute(
        delete(EtfTopHoldingRecord).where(EtfTopHoldingRecord.etf_id == etf_id)
    )
