"""Application port for the revenue-segments live source.

The abstraction the read path depends on for a *live* source of a company's revenue
disaggregation — implemented by the SEC EDGAR adapter and the DB-cache decorator in
``app/stocks/adapters``. Dependency Inversion: the core reads through this interface, never
SEC/httpx directly, so the source is swappable and the tests run offline against a
hand-written fake. The *persistence* seam is separate — the repository port lives in
``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.revenue_segments.entities import RevenueSegmentation


class RevenueSegmentsProvider(ABC):
    """A gateway for a company's revenue disaggregation (segments / products / geographies)."""

    @abstractmethod
    def get_revenue_segments(self, symbol: str) -> RevenueSegmentation:
        """Return the revenue segmentation for the (already-normalized) symbol.

        Returns an ``is_empty`` segmentation when the source covers the symbol but it reports
        no disaggregation (a single-segment filer, or a foreign issuer whose filing shape we
        don't parse) — "no breakdown" is not an error for this best-effort feature.

        Raises:
            StockNotFound: the symbol is not covered by the source (no matching filer).
            StockDataUnavailable: the upstream source failed (transport / bad response).
        """
        raise NotImplementedError
