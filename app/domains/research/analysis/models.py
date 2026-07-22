from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AnalysisCacheRecord(Base):
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
