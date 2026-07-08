"""Application ports for the ETF live sources.

The abstractions the sync use case depends on: the *screen* (the bulk top-ETF set) and the
per-fund *profile* lookup (the enrichment pass — the bulk screen carries none of a fund's profile,
exactly like the stock universe's sector). Both are implemented by yfinance adapters in
``app/stocks/adapters``. Dependency Inversion — the core screens/enriches through these interfaces,
never a vendor directly, so the sources are swappable and the tests run offline against
hand-written fakes. The *persistence* seam is separate — the repository ports live in
``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.etfs.entities import EtfProfile, ScreenedEtf


class EtfScreener(ABC):
    """A gateway for screening the US ETF market (every fund at/above an AUM floor)."""

    @abstractmethod
    def screen(self, *, min_net_assets: float) -> tuple[ScreenedEtf, ...]:
        """Return every US ETF with net assets (AUM) at/above ``min_net_assets`` (whole dollars).

        One bulk read of the floor-defined set the sync persists — the ETF analogue of the stock
        universe's market-cap floor. Order is unspecified; the read side sorts, so callers must
        not rely on it. Carries none of a fund's profile — that's the enrichment pass's job
        (``EtfProfileProvider``).

        Raises:
            StockDataUnavailable: the upstream screen failed. The sync treats this as a lost
                round — it skips the write rather than acting on a partial or empty result — so
                an adapter must raise here rather than return a half-populated screen.
        """
        raise NotImplementedError


class EtfProfileProvider(ABC):
    """A gateway for one fund's full profile — what the sync's enrichment pass persists and the
    detail view then serves.

    Reads the fund facts that live only on Yahoo's per-ticker surfaces (category, fund family, NAV,
    dividend yield, trailing returns, description, top holdings, sector weightings) — none of which
    the bulk screen carries. Separate from ``EtfScreener`` (the bulk AUM screen): the profile is a
    per-ticker read, filled a fund at a time. Dependency Inversion as ever — the core asks for a
    profile in domain terms and the yfinance adapter is the only thing that knows Yahoo backs it.
    Because ``category`` rides the same ``.info`` blob as the rest of the profile, the sync reads it
    here too, rather than through a second per-ticker call.

    Contract — **raises on a hard failure**: this is the sync's primary per-fund source, and the
    caller must be able to tell a blocked/failed fetch (leave the fund's stored rows alone, retry
    next run) from a fund Yahoo simply carries little data for (persist what came back). So a hard
    upstream failure — an outage or a data-centre-IP block that leaves the fund unreadable — raises
    ``StockDataUnavailable``; a fund that *is* reachable but sparse comes back as an ``EtfProfile``
    with whatever fields Yahoo served (the missing ones ``None`` / empty). Field-level gaps never
    raise — only an unreadable fund does.
    """

    @abstractmethod
    def get_profile(self, symbol: str) -> EtfProfile:
        """Return ``symbol``'s (already-normalized) fund profile.

        Percent figures on the returned profile are already normalized to human percent. A
        reachable-but-sparse fund yields a partial ``EtfProfile``; only an unreadable fund raises.

        Raises:
            StockDataUnavailable: the upstream lookup hard-failed (an outage or a data-centre-IP
                block). The sync counts it as a lost fund for the run, leaves its stored profile
                untouched, and moves on; the next run retries it.
        """
        raise NotImplementedError
