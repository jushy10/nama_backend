import logging
from datetime import datetime, timezone
from typing import Callable, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.stocks.ai.analysis.entities import (
    Confidence,
    EarningsAnalysis,
    EarningsTrend,
    FundamentalsAnalysis,
    FundamentalsVerdict,
    MarketIndexReturn,
    MarketPeriod,
    MarketPeriodHighlight,
    MarketSummary,
    MarketTone,
    RatingsAnalysis,
    RatingsVerdict,
    SectorAnalysis,
    SectorHighlight,
)
from app.stocks.ai.analysis.models import AnalysisCacheRecord
from app.stocks.ai.analysis.ports import AiAnalysisCache

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The columns a codec may write — everything but the identity (id/kind/symbol). An
# upsert overwrites all of them from the freshly-built row, so a column a kind doesn't
# use is (re)set to its None/default, keeping a row consistent with exactly one shape.
_MUTABLE_COLUMNS = (
    "recommendation",
    "confidence",
    "thesis",
    "strengths",
    "risks",
    "sections",
    "verdict",
    "findings",
    "details",
    "model",
    "generated_at",
)


class SqlAiAnalysisCache(AiAnalysisCache[T], Generic[T]):
    def __init__(
        self,
        session: Session,
        kind: str,
        to_row: Callable[[str, str, T], AnalysisCacheRecord],
        from_row: Callable[[AnalysisCacheRecord], T | None],
    ) -> None:
        self._session = session
        self._kind = kind
        self._to_row = to_row
        self._from_row = from_row

    def get(self, key: str) -> T | None:
        try:
            row = self._session.execute(
                select(AnalysisCacheRecord).where(
                    AnalysisCacheRecord.kind == self._kind,
                    AnalysisCacheRecord.symbol == key,
                )
            ).scalar_one_or_none()
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "ai analysis cache read failed for %s/%s", self._kind, key, exc_info=True
            )
            return None
        if row is None:
            return None
        return self._from_row(row)

    def put(self, key: str, analysis: T) -> None:
        try:
            row = self._session.execute(
                select(AnalysisCacheRecord).where(
                    AnalysisCacheRecord.kind == self._kind,
                    AnalysisCacheRecord.symbol == key,
                )
            ).scalar_one_or_none()
            fresh = self._to_row(self._kind, key, analysis)
            if row is None:
                self._session.add(fresh)
            else:
                for column in _MUTABLE_COLUMNS:
                    setattr(row, column, getattr(fresh, column))
            self._session.commit()
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "ai analysis cache write failed for %s/%s",
                self._kind,
                key,
                exc_info=True,
            )
            self._session.rollback()


# --- per-kind constructors (the composition root wires these) ---------------------
#
# Each pairs a *kind* string with its codec so both live in one place (the codecs stay
# private to this module). The router builds one per request session.


def earnings_analysis_cache(session: Session) -> SqlAiAnalysisCache[EarningsAnalysis]:
    return SqlAiAnalysisCache(session, "earnings", _earnings_to_row, _earnings_from_row)


def ratings_analysis_cache(session: Session) -> SqlAiAnalysisCache[RatingsAnalysis]:
    return SqlAiAnalysisCache(session, "ratings", _ratings_to_row, _ratings_from_row)


def fundamentals_analysis_cache(
    session: Session,
) -> SqlAiAnalysisCache[FundamentalsAnalysis]:
    return SqlAiAnalysisCache(
        session, "fundamentals", _fundamentals_to_row, _fundamentals_from_row
    )


def sector_analysis_cache(session: Session) -> SqlAiAnalysisCache[SectorAnalysis]:
    return SqlAiAnalysisCache(session, "sector", _sector_to_row, _sector_from_row)


def market_summary_cache(session: Session) -> SqlAiAnalysisCache[MarketSummary]:
    return SqlAiAnalysisCache(session, "market", _market_to_row, _market_from_row)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _opt_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _earnings_to_row(kind: str, key: str, a: EarningsAnalysis) -> AnalysisCacheRecord:
    return AnalysisCacheRecord(
        kind=kind,
        symbol=key,
        thesis=a.summary,
        verdict=a.trend.value,
        findings=list(a.highlights),
        model=a.model,
        generated_at=a.generated_at,
    )


def _earnings_from_row(row: AnalysisCacheRecord) -> EarningsAnalysis | None:
    try:
        trend = EarningsTrend(row.verdict)
    except ValueError:
        return None
    return EarningsAnalysis(
        symbol=row.symbol,
        summary=row.thesis,
        trend=trend,
        highlights=tuple(row.findings or ()),
        model=row.model,
        generated_at=_utc(row.generated_at),
    )


def _ratings_to_row(kind: str, key: str, a: RatingsAnalysis) -> AnalysisCacheRecord:
    return AnalysisCacheRecord(
        kind=kind,
        symbol=key,
        thesis=a.summary,
        verdict=a.verdict.value,
        confidence=a.confidence.value,
        findings=list(a.findings),
        model=a.model,
        generated_at=a.generated_at,
    )


def _ratings_from_row(row: AnalysisCacheRecord) -> RatingsAnalysis | None:
    try:
        verdict = RatingsVerdict(row.verdict)
        confidence = Confidence(row.confidence)
    except ValueError:
        return None
    return RatingsAnalysis(
        symbol=row.symbol,
        verdict=verdict,
        confidence=confidence,
        summary=row.thesis,
        findings=tuple(row.findings or ()),
        model=row.model,
        generated_at=_utc(row.generated_at),
    )


def _fundamentals_to_row(
    kind: str, key: str, a: FundamentalsAnalysis
) -> AnalysisCacheRecord:
    return AnalysisCacheRecord(
        kind=kind,
        symbol=key,
        thesis=a.summary,
        verdict=a.verdict.value,
        confidence=a.confidence.value,
        findings=list(a.findings),
        model=a.model,
        generated_at=a.generated_at,
    )


def _fundamentals_from_row(row: AnalysisCacheRecord) -> FundamentalsAnalysis | None:
    try:
        verdict = FundamentalsVerdict(row.verdict)
        confidence = Confidence(row.confidence)
    except ValueError:
        return None
    return FundamentalsAnalysis(
        symbol=row.symbol,
        verdict=verdict,
        confidence=confidence,
        summary=row.thesis,
        findings=tuple(row.findings or ()),
        model=row.model,
        generated_at=_utc(row.generated_at),
    )


# --- sector (market-wide) ---------------------------------------------------------


def _highlight_to_json(h: SectorHighlight) -> dict:
    return {
        "sector": h.sector,
        "symbol": h.symbol,
        "change_percent": h.change_percent,
        "note": h.note,
    }


def _highlights_from_json(raw) -> tuple[SectorHighlight, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        SectorHighlight(
            sector=str(item.get("sector", "")),
            symbol=str(item.get("symbol", "")),
            change_percent=_opt_float(item.get("change_percent")),
            note=str(item.get("note", "")),
        )
        for item in raw
        if isinstance(item, dict)
    )


def _sector_to_row(kind: str, key: str, a: SectorAnalysis) -> AnalysisCacheRecord:
    return AnalysisCacheRecord(
        kind=kind,
        symbol=key,
        thesis=a.summary,
        verdict=a.tone.value,
        details={
            "leaders": [_highlight_to_json(h) for h in a.leaders],
            "laggards": [_highlight_to_json(h) for h in a.laggards],
        },
        model=a.model,
        generated_at=a.generated_at,
    )


def _sector_from_row(row: AnalysisCacheRecord) -> SectorAnalysis | None:
    try:
        tone = MarketTone(row.verdict)
    except ValueError:
        return None
    details = row.details if isinstance(row.details, dict) else {}
    return SectorAnalysis(
        summary=row.thesis,
        tone=tone,
        leaders=_highlights_from_json(details.get("leaders")),
        laggards=_highlights_from_json(details.get("laggards")),
        model=row.model,
        generated_at=_utc(row.generated_at),
    )


# --- market (market-wide) ---------------------------------------------------------


def _period_to_json(p: MarketPeriodHighlight) -> dict:
    return {
        "period": p.period.value,
        "note": p.note,
        "indexes": [
            {"name": ix.name, "symbol": ix.symbol, "change_percent": ix.change_percent}
            for ix in p.indexes
        ],
    }


def _index_returns_from_json(raw) -> tuple[MarketIndexReturn, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        MarketIndexReturn(
            name=str(item.get("name", "")),
            symbol=str(item.get("symbol", "")),
            change_percent=_opt_float(item.get("change_percent")),
        )
        for item in raw
        if isinstance(item, dict)
    )


def _periods_from_json(raw) -> tuple[MarketPeriodHighlight, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[MarketPeriodHighlight] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            period = MarketPeriod(item.get("period"))
        except ValueError:
            continue
        out.append(
            MarketPeriodHighlight(
                period=period,
                note=str(item.get("note", "")),
                indexes=_index_returns_from_json(item.get("indexes")),
            )
        )
    return tuple(out)


def _market_to_row(kind: str, key: str, a: MarketSummary) -> AnalysisCacheRecord:
    return AnalysisCacheRecord(
        kind=kind,
        symbol=key,
        thesis=a.summary,
        verdict=a.tone.value,
        details={"periods": [_period_to_json(p) for p in a.periods]},
        model=a.model,
        generated_at=a.generated_at,
    )


def _market_from_row(row: AnalysisCacheRecord) -> MarketSummary | None:
    try:
        tone = MarketTone(row.verdict)
    except ValueError:
        return None
    details = row.details if isinstance(row.details, dict) else {}
    return MarketSummary(
        summary=row.thesis,
        tone=tone,
        periods=_periods_from_json(details.get("periods")),
        model=row.model,
        generated_at=_utc(row.generated_at),
    )
