"""Application port for the analyst-estimates live source.

The abstraction the read path depends on for a *live* source of forward estimates —
implemented by the FMP adapter and the cache decorators in ``app/stocks/adapters``.
Dependency Inversion: the core reads through this interface, never FMP directly, so the
vendor is swappable and the tests run offline against a hand-written fake. The
*persistence* seam is separate — the repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.entities import AnalystEstimates


class AnalystEstimatesProvider(ABC):
    """A gateway for a stock's forward analyst consensus estimates.

    Forward EPS/revenue expectations come from a sell-side estimates vendor — not
    the price feed or company filings — so this carries consensus *estimates*, never
    reported actuals. Best-effort enrichment on the stock snapshot (it backs the
    forward P/E and forward P/S), so a failure here must not sink the price response.
    """

    @abstractmethod
    def get_estimates(self, symbol: str) -> AnalystEstimates:
        """Return forward consensus estimates for the (already-normalized) symbol.

        Returns an ``is_empty`` ``AnalystEstimates`` (all ``None``) when the source
        covers no forward fiscal year for the symbol — "no data" is not an error for
        best-effort enrichment.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError
