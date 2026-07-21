import logging

from app.stocks.company.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.company.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.company.earnings.annual.repository import AnnualEarningsRepository
from app.stocks.company.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.company.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.company.earnings.quarterly.repository import QuarterlyEarningsRepository
from app.stocks.company.recommendations.entities import (
    AnalystRatingChanges,
    AnalystRecommendations,
)
from app.stocks.company.recommendations.ports import (
    RatingChangeProvider,
    RecommendationProvider,
)
from app.stocks.company.recommendations.repository import (
    RatingChangesRepository,
    RecommendationsRepository,
)

logger = logging.getLogger(__name__)


class DbOnlyQuarterlyEarningsProvider(QuarterlyEarningsProvider):
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


class DbOnlyAnnualEarningsProvider(AnnualEarningsProvider):
    def __init__(self, repo: AnnualEarningsRepository) -> None:
        self._repo = repo

    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        try:
            stored = self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — best-effort context, never sink the analysis
            logger.warning("annual cache read failed for %s", symbol, exc_info=True)
            stored = None
        return stored if stored is not None else AnnualEarningsTimeline(symbol, ())


class DbOnlyRecommendationsProvider(RecommendationProvider):
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


class DbOnlyRatingChangesProvider(RatingChangeProvider):
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