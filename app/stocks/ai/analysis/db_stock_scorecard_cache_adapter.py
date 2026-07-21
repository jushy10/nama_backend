import logging
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.stocks.ai.analysis.entities import (
    Confidence,
    Recommendation,
    ScorecardSection,
    SectionMetric,
    SectionStance,
    StockScorecard,
)
from app.stocks.ai.analysis.models import AnalysisCacheRecord
from app.stocks.ai.analysis.interfaces import StockScorecardCacheAdapter

logger = logging.getLogger(__name__)


class DbStockScorecardCacheAdapter(StockScorecardCacheAdapter):
    def __init__(self, session: Session, kind: str = "stock") -> None:
        self._session = session
        self._kind = kind

    def get(self, symbol: str) -> StockScorecard | None:
        try:
            row = self._session.execute(
                select(AnalysisCacheRecord).where(
                    AnalysisCacheRecord.kind == self._kind,
                    AnalysisCacheRecord.symbol == symbol,
                )
            ).scalar_one_or_none()
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "scorecard cache read failed for %s/%s",
                self._kind,
                symbol,
                exc_info=True,
            )
            return None
        if row is None:
            return None
        return _to_entity(row)

    def put(self, scorecard: StockScorecard) -> None:
        try:
            row = self._session.execute(
                select(AnalysisCacheRecord).where(
                    AnalysisCacheRecord.kind == self._kind,
                    AnalysisCacheRecord.symbol == scorecard.symbol,
                )
            ).scalar_one_or_none()
            if row is None:
                self._session.add(_to_row(self._kind, scorecard))
            else:
                _apply(row, scorecard)
            self._session.commit()
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "scorecard cache write failed for %s/%s",
                self._kind,
                scorecard.symbol,
                exc_info=True,
            )
            self._session.rollback()


def _to_entity(row: AnalysisCacheRecord) -> StockScorecard | None:
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
    return StockScorecard(
        symbol=row.symbol,
        recommendation=recommendation,
        confidence=confidence,
        thesis=row.thesis,
        sections=_sections_from_json(row.sections),
        model=row.model,
        generated_at=generated_at,
    )


def _sections_from_json(raw) -> tuple[ScorecardSection, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[ScorecardSection] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            stance = SectionStance(item.get("stance"))
        except ValueError:
            stance = SectionStance.NEUTRAL
        metrics = tuple(
            SectionMetric(str(m.get("label", "")), str(m.get("value", "")))
            for m in (item.get("metrics") or [])
            if isinstance(m, dict)
        )
        out.append(
            ScorecardSection(
                key=str(item.get("key", "")),
                title=str(item.get("title", "")),
                stance=stance,
                label=str(item.get("label", "")),
                summary=str(item.get("summary", "")),
                metrics=metrics,
            )
        )
    return tuple(out)


def _sections_to_json(sections: tuple[ScorecardSection, ...]) -> list:
    return [
        {
            "key": s.key,
            "title": s.title,
            "stance": s.stance.value,
            "label": s.label,
            "summary": s.summary,
            "metrics": [{"label": m.label, "value": m.value} for m in s.metrics],
        }
        for s in sections
    ]


def _to_row(kind: str, scorecard: StockScorecard) -> AnalysisCacheRecord:
    return AnalysisCacheRecord(
        kind=kind,
        symbol=scorecard.symbol,
        recommendation=scorecard.recommendation.value,
        confidence=scorecard.confidence.value,
        thesis=scorecard.thesis,
        sections=_sections_to_json(scorecard.sections),
        model=scorecard.model,
        generated_at=scorecard.generated_at,
    )


def _apply(row: AnalysisCacheRecord, scorecard: StockScorecard) -> None:
    row.recommendation = scorecard.recommendation.value
    row.confidence = scorecard.confidence.value
    row.thesis = scorecard.thesis
    row.sections = _sections_to_json(scorecard.sections)
    row.model = scorecard.model
    row.generated_at = scorecard.generated_at
