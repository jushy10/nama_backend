"""Interface Adapter: the generic DB-backed AI-analysis result cache.

Implements the ``AiAnalysisCache`` port over the shared ``investment_analysis_cache``
table (``models.py``) for the five newer AI reads — earnings, ratings, fundamentals,
sector and market — that the two hand-written caches (``SqlInvestmentAnalysisCache`` for
the ETF analysis, ``SqlStockScorecardCache`` for the stock scorecard) don't cover.

Rather than five more near-identical adapters, there is **one** generic
``SqlAiAnalysisCache`` parameterized by a *kind* and a **codec** — a
``(to_row, from_row)`` pair that maps a specific analysis entity to and from a cache
row. The get/put/upsert skeleton (the same select-then-update/insert the two existing
adapters duplicate) lives once here; each kind supplies only its column mapping. The
codecs are the module-level ``_<kind>_to_row`` / ``_<kind>_from_row`` functions below.

Being a cache, both operations are deliberately best-effort (the port's contract):

- ``get`` treats *any* failure — a DB hiccup, or a row whose stored enum no longer
  parses — as a miss and returns ``None``, so the caller cleanly regenerates.
- ``put`` upserts by ``(kind, key)`` (select-then-update/insert, so it stays
  dialect-agnostic across SQLite and Postgres) and swallows write failures — the
  caller already holds the freshly-generated answer.

Neither ever raises, so a cache problem can never sink an analysis request.
"""

import logging
from datetime import datetime, timezone
from typing import Callable, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.stocks.analysis.models import AnalysisCacheRecord
from app.stocks.entities import (
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
from app.stocks.ports import AiAnalysisCache

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
    """Read-through cache storage for one *kind* of AI analysis, via an injected codec.

    ``to_row`` builds a fresh ``AnalysisCacheRecord`` from ``(kind, key, analysis)``;
    ``from_row`` maps a stored row back to the entity (or ``None`` if it no longer
    parses). Bound to a *kind* so, e.g., an ``earnings`` read never collides with a
    ``ratings`` one for the same symbol.
    """

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


# --- shared helpers ---------------------------------------------------------------


def _utc(dt: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive stamp. SQLite drops tzinfo (no native tz type) and the
    figures are always stored in UTC, so the use case's age comparison needs it back."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _opt_float(value) -> float | None:
    """Coerce a stored JSON number to ``float`` (or ``None``), tolerating a malformed
    value — best-effort, like the rest of the cache."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- earnings ---------------------------------------------------------------------


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


# --- ratings ----------------------------------------------------------------------


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


# --- fundamentals -----------------------------------------------------------------


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
    """Rebuild the highlight tuple from stored JSON, skipping any malformed entry
    (best-effort, mirroring ``_sections_from_json`` in the scorecard cache)."""
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
    """Rebuild the period-highlight tuple from stored JSON, skipping any entry whose
    period enum no longer parses (best-effort)."""
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
