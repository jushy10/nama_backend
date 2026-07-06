"""Application ports for the ETF live sources.

The abstractions the sync use case depends on: the *screen* (the bulk top-ETF set) and the
per-ticker *category* lookup (the enrichment pass — the bulk screen carries no category, exactly
like the stock universe's sector). Both are implemented by yfinance adapters in
``app/stocks/adapters``. Dependency Inversion — the core screens/classifies through these
interfaces, never a vendor directly, so the sources are swappable and the tests run offline
against hand-written fakes. The *persistence* seam is separate — the repository ports live in
``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.etfs.entities import EtfClassification, ScreenedEtf


class EtfScreener(ABC):
    """A gateway for screening the US ETF market (every fund at/above an AUM floor)."""

    @abstractmethod
    def screen(self, *, min_net_assets: float) -> tuple[ScreenedEtf, ...]:
        """Return every US ETF with net assets (AUM) at/above ``min_net_assets`` (whole dollars).

        One bulk read of the floor-defined set the sync persists — the ETF analogue of the stock
        universe's market-cap floor. Order is unspecified; the read side sorts, so callers must
        not rely on it. Carries no ``category`` — that's the enrichment pass's job
        (``EtfCategoryProvider``).

        Raises:
            StockDataUnavailable: the upstream screen failed. The sync treats this as a lost
                round — it skips the write rather than acting on a partial or empty result — so
                an adapter must raise here rather than return a half-populated screen.
        """
        raise NotImplementedError


class EtfCategoryProvider(ABC):
    """A gateway for one ETF's category classification.

    Separate from ``EtfScreener`` because the bulk screen carries no category — it lives only on
    the per-ticker ``.info`` surface — so the sync's enrichment pass fills it a fund at a time
    through this port (the ETF analogue of ``CompanyClassificationProvider`` for stock sectors).
    Dependency Inversion, as ever: the core asks for a category in domain terms and never touches
    a vendor directly, so the source is swappable and the tests run offline against a fake.
    """

    @abstractmethod
    def get_category(self, symbol: str) -> EtfClassification:
        """Return ``symbol``'s category (as a snake_case slug).

        A fund the source doesn't categorise yields an ``EtfClassification`` with ``category``
        ``None`` rather than an error — best-effort enrichment.

        Raises:
            StockDataUnavailable: the upstream lookup failed (an outage or a data-centre-IP
                block). The sync counts it as a lost fund for the run and moves on; the next run
                retries it.
        """
        raise NotImplementedError
