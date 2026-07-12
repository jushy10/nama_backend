"""Interface Adapter: the DB-backed AI-analysis result cache.

Implements ``InvestmentAnalysisCache`` over the ``investment_analysis_cache`` table
(``models.py``), mapping rows to and from the ``InvestmentAnalysis`` entity. Now the
**ETF** analysis's cache — the stock endpoint moved to the sectioned
``SqlStockScorecardCache`` (same table, ``sections`` column). One instance is bound to
a *kind* (``"etf"`` in practice) and the request session, so a fund never collides
with a stock of the same ticker.

Being a cache, both operations are deliberately best-effort (the port's contract):

- ``get`` treats *any* failure — a DB hiccup, or a row whose stored enum no longer
  parses — as a miss and returns ``None``, so the caller cleanly regenerates.
- ``put`` upserts by ``(kind, symbol)`` (select-then-update/insert, so it stays
  dialect-agnostic across SQLite and Postgres) and swallows write failures — the
  caller already holds the freshly-generated answer.

Neither ever raises, so a cache problem can never sink an analysis request.
"""

import logging
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.stocks.analysis.models import AnalysisCacheRecord
from app.stocks.analysis.entities import Confidence, InvestmentAnalysis, Recommendation
from app.stocks.analysis.ports import InvestmentAnalysisCache

logger = logging.getLogger(__name__)


class SqlInvestmentAnalysisCache(InvestmentAnalysisCache):
    """Read-through cache storage for one *kind* of analysis (stock or ETF)."""

    def __init__(self, session: Session, kind: str) -> None:
        self._session = session
        self._kind = kind

    def get(self, symbol: str) -> InvestmentAnalysis | None:
        try:
            row = self._session.execute(
                select(AnalysisCacheRecord).where(
                    AnalysisCacheRecord.kind == self._kind,
                    AnalysisCacheRecord.symbol == symbol,
                )
            ).scalar_one_or_none()
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "analysis cache read failed for %s/%s", self._kind, symbol, exc_info=True
            )
            return None
        if row is None:
            return None
        return _to_entity(row)

    def put(self, analysis: InvestmentAnalysis) -> None:
        try:
            row = self._session.execute(
                select(AnalysisCacheRecord).where(
                    AnalysisCacheRecord.kind == self._kind,
                    AnalysisCacheRecord.symbol == analysis.symbol,
                )
            ).scalar_one_or_none()
            if row is None:
                self._session.add(_to_row(self._kind, analysis))
            else:
                _apply(row, analysis)
            self._session.commit()
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "analysis cache write failed for %s/%s",
                self._kind,
                analysis.symbol,
                exc_info=True,
            )
            self._session.rollback()


def _to_entity(row: AnalysisCacheRecord) -> InvestmentAnalysis | None:
    """Map a stored row onto the entity, or ``None`` if it no longer parses.

    A row written by an older build could carry a recommendation/confidence value
    this build no longer knows; rather than raise, treat it as a miss so the caller
    regenerates a valid one."""
    try:
        recommendation = Recommendation(row.recommendation)
        confidence = Confidence(row.confidence)
    except ValueError:
        return None
    generated_at = row.generated_at
    # SQLite drops tzinfo (it has no native tz type); the figures are always stored
    # in UTC, so re-attach it for a correct age comparison in the use case.
    if generated_at is not None and generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return InvestmentAnalysis(
        symbol=row.symbol,
        recommendation=recommendation,
        confidence=confidence,
        thesis=row.thesis,
        strengths=tuple(row.strengths or ()),
        risks=tuple(row.risks or ()),
        model=row.model,
        generated_at=generated_at,
    )


def _to_row(kind: str, analysis: InvestmentAnalysis) -> AnalysisCacheRecord:
    return AnalysisCacheRecord(
        kind=kind,
        symbol=analysis.symbol,
        recommendation=analysis.recommendation.value,
        confidence=analysis.confidence.value,
        thesis=analysis.thesis,
        strengths=list(analysis.strengths),
        risks=list(analysis.risks),
        model=analysis.model,
        generated_at=analysis.generated_at,
    )


def _apply(row: AnalysisCacheRecord, analysis: InvestmentAnalysis) -> None:
    row.recommendation = analysis.recommendation.value
    row.confidence = analysis.confidence.value
    row.thesis = analysis.thesis
    row.strengths = list(analysis.strengths)
    row.risks = list(analysis.risks)
    row.model = analysis.model
    row.generated_at = analysis.generated_at
