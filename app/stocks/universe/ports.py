"""Application port for the stock-universe live source (the screener).

The abstraction the sync use case depends on for a *live* screen of the US market: every
listed company at/above a market-cap floor. Implemented by the Nasdaq adapter in
``app/stocks/adapters``. Dependency Inversion — the core screens through this interface,
never a vendor directly, so the source is swappable (Nasdaq today, yfinance's ``yf.screen``
tomorrow) and the tests run offline against a hand-written fake. The *persistence* seam is
separate — the repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.stocks.universe.entities import (
    CompanyClassification,
    ScreenedStock,
    ScreenIntent,
)


class StockScreener(ABC):
    """A gateway for screening one market's listings by market capitalisation."""

    @abstractmethod
    def screen(
        self, *, min_market_cap: float, region: str = "us"
    ) -> tuple[ScreenedStock, ...]:
        """Return every stock in ``region`` at/above ``min_market_cap`` — in the market's
        **native trading currency**, not a converted one.

        One bulk read of the market, filtered to the floor — the investable universe the sync
        persists. ``region`` is an ISO-2 market code (``"us"`` default, ``"ca"`` for the
        Canadian TSX/TSXV listings); the floor is applied in that market's own currency (Yahoo
        screens each quote in its native currency), so ``min_market_cap=1e9`` is $1B USD for
        ``us`` and $1B CAD for ``ca``. Each returned ``ScreenedStock`` carries the ``country`` /
        ``currency`` that unit belongs to. Order is unspecified; callers must not rely on it.

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


class ScreenerQueryTranslator(ABC):
    """A gateway that turns a plain-English screen request into structured search filters.

    The abstraction the ``AiScreenStocks`` use case depends on so a user can screen the
    universe by asking ("mega-cap technology stocks", "top S&P 500 names by revenue growth")
    instead of setting each control by hand. Dependency Inversion as ever — the core hands
    the request (and the vocabulary of valid slugs) to this interface and gets back a
    ``ScreenIntent`` in domain terms, never touching an LLM/vendor directly, so the source is
    swappable (Claude on Bedrock today) and the tests run offline against a hand-written fake.

    The translation is *primary* data for the AI-screen endpoint (its reason to exist), so
    unlike the enrichment ports a failure here **propagates** rather than degrading to a
    neutral result.
    """

    @abstractmethod
    def translate(
        self,
        query: str,
        *,
        sectors: Sequence[str],
        industries: Sequence[str],
    ) -> ScreenIntent:
        """Translate ``query`` into a ``ScreenIntent`` of search filters.

        ``sectors`` / ``industries`` are the slugs currently present in the universe — the
        allowed vocabulary the translator constrains its sector/industry choices to, so the
        result maps onto values the search can actually match (an implementation may also
        leave those filters unset when the request names neither). The intent is *advisory*:
        the use case still normalizes every field through the ordinary search, so an
        off-vocabulary or nonsensical value degrades to "matches nothing", never an error.

        Raises:
            StockDataUnavailable: the upstream translation failed (a model/vendor error).
                The endpoint surfaces it as a 502 — the request couldn't be understood this
                time, distinct from a well-understood request that simply matched no stocks.
        """
        raise NotImplementedError
