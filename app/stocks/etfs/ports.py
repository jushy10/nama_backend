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

from app.stocks.etfs.entities import EtfClassification, EtfProfile, ScreenedEtf


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


class EtfProfileProvider(ABC):
    """A gateway for one fund's rich profile — the enrichment the ETF *detail* endpoint layers on
    top of the live quote and the stored ``etfs`` facts.

    Reads the fund facts that live only on Yahoo's per-ticker surfaces (fund family, NAV, trailing
    returns, description, holdings, sector weightings) — the ones the bulk screen and the ``etfs``
    table don't keep. Separate from ``EtfCategoryProvider`` (which reads just the one category slug
    the sync persists): the detail view wants the whole profile, live per request, not a single
    stored column. Dependency Inversion as ever — the core asks for a profile in domain terms and
    the yfinance adapter is the only thing that knows Yahoo backs it.

    Deliberately **total**, unlike the other ports: this is best-effort enrichment on a view whose
    primary source is the quote, so a blocked/failed Yahoo read must not sink the request. The
    contract is therefore to *never raise* — an outage, an IP block, or an uncovered fund all come
    back as an empty ``EtfProfile`` (all ``None`` / empty lists), which the endpoint serves around.
    """

    @abstractmethod
    def get_profile(self, symbol: str) -> EtfProfile:
        """Return ``symbol``'s (already-normalized) fund profile, best-effort.

        Never raises: any vendor failure or missing field degrades to an empty ``EtfProfile``
        rather than an error, so the detail endpoint still returns 200 with the quote + stored
        facts. Percent figures on the returned profile are already normalized to human percent.
        """
        raise NotImplementedError
