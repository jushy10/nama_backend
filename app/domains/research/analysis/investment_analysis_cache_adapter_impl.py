import logging
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domains.research.analysis.models import AnalysisCacheRecord
from app.domains.research.analysis.entities import Confidence, InvestmentAnalysis, Recommendation
from app.domains.research.analysis.interfaces import InvestmentAnalysisCacheAdapter

logger = logging.getLogger(__name__)


class InvestmentAnalysisCacheAdapterImpl(InvestmentAnalysisCacheAdapter):
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
