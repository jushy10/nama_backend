import logging

from app.domains.financials.earnings.annual.entities import AnnualEarningsTimeline
from app.domains.financials.earnings.annual.interfaces import AnnualEarningsAdapter
from app.domains.financials.earnings.annual.interfaces import AnnualEarningsRepositoryAdapter
from app.domains.financials.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.domains.financials.earnings.quarterly.interfaces import QuarterlyEarningsAdapter
from app.domains.financials.earnings.quarterly.repository import QuarterlyEarningsRepository
from app.domains.coverage.recommendations.entities import (
    AnalystRatingChanges,
    AnalystRecommendations,
)
from app.domains.coverage.recommendations.interfaces import (
    RatingChangeAdapter,
    RecommendationAdapter,
)
from app.domains.coverage.recommendations.repository import (
    RatingChangesRepository,
    RecommendationsRepository,
)

logger = logging.getLogger(__name__)


class QuarterlyEarningsAdapterImpl(QuarterlyEarningsAdapter):
    def __init__(self, repo: QuarterlyEarningsRepository) -> None:
        self._repo = repo

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        try:
            stored = self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — best-effort context, never sink the analysis
            logger.warning(
                "quarterly cache read failed for %s", symbol, exc_info=True
            )
            stored = None
        return stored if stored is not None else QuarterlyEarningsTimeline(symbol, ())


class AnnualEarningsAdapterImpl(AnnualEarningsAdapter):
    def __init__(self, repo: AnnualEarningsRepositoryAdapter) -> None:
        self._repo = repo

    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        try:
            stored = self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — best-effort context, never sink the analysis
            logger.warning("annual cache read failed for %s", symbol, exc_info=True)
            stored = None
        return stored if stored is not None else AnnualEarningsTimeline(symbol, ())


class RecommendationAdapterImpl(RecommendationAdapter):
    def __init__(self, repo: RecommendationsRepository) -> None:
        self._repo = repo

    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        try:
            stored = self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — best-effort context, never sink the analysis
            logger.warning(
                "recommendations cache read failed for %s", symbol, exc_info=True
            )
            stored = None
        return stored if stored is not None else AnalystRecommendations(symbol, ())


class RatingChangeAdapterImpl(RatingChangeAdapter):
    def __init__(self, repo: RatingChangesRepository) -> None:
        self._repo = repo

    def get_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        try:
            stored = self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — best-effort context, never sink the analysis
            logger.warning(
                "rating-changes cache read failed for %s", symbol, exc_info=True
            )
            stored = None
        return stored if stored is not None else AnalystRatingChanges(symbol, ())