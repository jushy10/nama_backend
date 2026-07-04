"""Application port for the stock-universe live source (the screener).

The abstraction the sync use case depends on for a *live* screen of the US market: every
listed company at/above a market-cap floor. Implemented by the Nasdaq adapter in
``app/stocks/adapters``. Dependency Inversion — the core screens through this interface,
never a vendor directly, so the source is swappable (Nasdaq today, yfinance's ``yf.screen``
tomorrow) and the tests run offline against a hand-written fake. The *persistence* seam is
separate — the repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.universe.entities import CompanyClassification, ScreenedStock


class StockScreener(ABC):
    """A gateway for screening the US market by market capitalisation."""

    @abstractmethod
    def screen(self, *, min_market_cap: float) -> tuple[ScreenedStock, ...]:
        """Return every US-listed stock at/above ``min_market_cap`` (whole dollars).

        One bulk read of the whole market, filtered to the floor — the investable
        universe the sync persists. Order is unspecified; callers must not rely on it.

        Raises:
            StockDataUnavailable: the upstream screen failed. The sync treats this as a
                lost round — it skips the reconcile rather than acting on a partial or
                empty result — so an adapter must raise here rather than return a
                half-populated screen.
        """
        raise NotImplementedError


class CompanyClassificationProvider(ABC):
    """A gateway for one stock's sector + industry classification.

    Separate from ``StockScreener`` because Yahoo's bulk screen carries no sector/industry —
    they live only on the per-ticker ``.info`` surface — so the sync's enrichment pass fills
    them a symbol at a time through this port. Dependency Inversion, as ever: the core asks
    for a classification in domain terms and never touches a vendor directly, so the source
    is swappable and the tests run offline against a hand-written fake.
    """

    @abstractmethod
    def get_classification(self, symbol: str) -> CompanyClassification:
        """Return ``symbol``'s sector + industry (as snake_case slugs).

        A symbol the source doesn't classify yields a ``CompanyClassification`` with both
        sides ``None`` rather than an error — best-effort enrichment.

        Raises:
            StockDataUnavailable: the upstream lookup failed (an outage or a data-centre-IP
                block). The sync counts it as a lost symbol for the run and moves on; the
                next run retries it.
        """
        raise NotImplementedError
