"""Database model for the AI-analysis result cache.

One tiny table, ``investment_analysis_cache``, holding the most recent AI
buy/hold/sell read per symbol so a repeat view (or a burst of viewers) skips the
expensive gather + model call. It is a **cache**, not a source of record: every
row is regenerated once its stored ``generated_at`` ages past the use case's TTL,
so nothing here is authoritative and a lost row just triggers one regeneration.

The table backs several shapes, told apart by ``kind``. The two original ones: the
**ETF** analysis's flat ``InvestmentAnalysis`` (``thesis`` + the ``strengths``/``risks``
bullet lists), and the **stock** endpoint's sectioned ``StockScorecard`` (``thesis`` +
the ``sections`` JSON, with the bullet columns left empty). Migration 0030 added five
more — ``earnings`` / ``ratings`` / ``fundamentals`` / ``sector`` / ``market`` — which
share a small set of generic columns (``verdict`` / ``findings`` / ``details``, plus the
reused ``thesis`` and ``confidence``) rather than a bespoke column each; their codecs
live in ``ai_analysis_cache_repository.py``. Each repository reads and writes only the
columns its shape uses. The market-wide kinds (``sector`` / ``market``) take no symbol,
so they key on a fixed sentinel ``symbol``.

Unlike the earnings time-series, this is **not** a child of the ``stocks`` anchor:
an analysis is served for any valid ticker (including ones the universe screen has
never touched), and forcing a ``stocks`` row per analysed symbol would leak
arbitrary tickers into the screened universe. So the cache stands alone, keyed by
its own ``(kind, symbol)`` — ``kind`` separating a stock read from a fund read that
happen to share a ticker. The stock and ETF analysers both write here (both produce
the same ``InvestmentAnalysis`` shape); the sector read, a different entity, does
not.

The concrete repository (``db_repository.py``) is the only caller; it maps this row
to and from the ``InvestmentAnalysis`` entity, so this layer deals only in rows and
columns. The schema is created by migration 0022.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AnalysisCacheRecord(Base):
    """A cached AI analysis — one row per ``(kind, symbol)``.

    ``id`` is a surrogate UUID; ``kind`` (``"stock"`` / ``"etf"``) + ``symbol`` are
    the lookup key (unique together). ``recommendation`` / ``confidence`` store the
    enum *values* (the same strings the API serves); ``thesis`` is free text;
    ``strengths`` / ``risks`` are short string lists kept as JSON (a handful of
    bullet points, not worth their own child table). ``model`` records which model
    produced the read and ``generated_at`` when — the latter is what the use case
    ages against its TTL to decide a hit is still fresh.

    ``sections`` (nullable JSON, migration 0027) holds the **stock** endpoint's
    sectioned scorecard — a list of ``{key, title, stance, label, summary,
    metrics:[{label, value}]}`` — and is null for the ETF rows, which use
    ``strengths`` / ``risks`` instead.
    """

    __tablename__ = "investment_analysis_cache"
    __table_args__ = (
        UniqueConstraint("kind", "symbol", name="uq_investment_analysis_cache_kind_symbol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    # recommendation/confidence/strengths/risks are the stock+ETF shapes' columns;
    # nullable (migration 0030) because the five newer kinds don't all carry them —
    # ratings/fundamentals reuse `confidence`, everyone else leaves these null.
    recommendation: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    strengths: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    risks: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    # The stock endpoint's sectioned scorecard (null for the ETF rows, which use the
    # strengths/risks bullet columns above instead).
    sections: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    # The five newer kinds' columns (migration 0030): `verdict` holds their headline
    # enum (earnings `trend`, ratings/fundamentals `verdict`, sector/market `tone`);
    # `findings` the flat takeaway list (earnings `highlights`, ratings/fundamentals
    # `findings`); `details` the market-wide nested structure (sector
    # `{leaders, laggards}`, market `{periods}`). All null for the stock/ETF rows.
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    findings: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
