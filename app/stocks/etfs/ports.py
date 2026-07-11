"""Application ports for the ETF live sources.

The abstractions the sync use case depends on: the *screen* (the bulk top-ETF set) and the
per-fund *profile* lookup (the enrichment pass — the bulk screen carries none of a fund's profile,
exactly like the stock universe's sector). Both are implemented by yfinance adapters in
``app/stocks/adapters``. Dependency Inversion — the core screens/enriches through these interfaces,
never a vendor directly, so the sources are swappable and the tests run offline against
hand-written fakes. The *persistence* seam is separate — the repository ports live in
``repository.py``.

``EtfAnalysisProvider`` is the odd one out — not a data source but the AI read of an already-built
detail card. It takes the assembled ``EtfDetail`` (quote + facts + profile) the use case gathered
and returns a plain-language buy/hold/sell ``InvestmentAnalysis``; its yfinance-free adapter is the
Bedrock one in ``app/stocks/adapters``. Same inversion — the core asks for an analysis in domain
terms, only the adapter knows a language model backs it.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.stocks.entities import InvestmentAnalysis
from app.stocks.etfs.entities import (
    EtfDetail,
    EtfProfile,
    EtfScreenIntent,
    ScreenedEtf,
)


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


class EtfAnalysisProvider(ABC):
    """A gateway for an AI-generated buy/hold/sell read on one fund.

    Unlike the other ports this is not a data *source* — the use case has already assembled the
    fund's ``EtfDetail`` (the live quote, the stored facts, and the best-effort profile), and this
    port turns that snapshot into a plain-language ``InvestmentAnalysis``. It is handed the whole
    ``EtfDetail`` rather than a symbol, so the adapter does no fetching of its own — it only reasons
    over what it's given. Dependency Inversion as ever: the core asks for an analysis in domain
    terms, and only the adapter knows a language model (Claude on Bedrock) backs it.
    """

    @abstractmethod
    def analyze(self, detail: EtfDetail) -> InvestmentAnalysis:
        """Return a balanced buy/hold/sell analysis of the fund described by ``detail``.

        The analysis must be grounded only in the figures on ``detail`` (price, size, cost, yield,
        returns, holdings, sector split) — never outside knowledge. Best-effort/absent fields are
        simply omitted from the model's view, so a thin detail yields a lower-confidence read.

        Raises:
            StockDataUnavailable: the analysis could not be produced — the model call failed or
                returned no usable structured result. The one error this port documents; the
                endpoint maps it to a 502.
        """
        raise NotImplementedError


class EtfScreenerQueryTranslator(ABC):
    """A gateway that turns a plain-English ETF-screen request into structured search filters.

    The ETF analogue of the stock universe's ``ScreenerQueryTranslator``: the abstraction the
    ``AiScreenEtfs`` use case depends on so a user can screen the fund set by asking ("cheap S&P
    500 index funds", "high-yield dividend ETFs", "gold funds by size") instead of setting each
    control by hand. Dependency Inversion as ever — the core hands the request (and the vocabulary
    of valid category slugs) to this interface and gets back an ``EtfScreenIntent`` in domain
    terms, never touching an LLM/vendor directly, so the source is swappable (Claude on Bedrock
    today) and the tests run offline against a hand-written fake.

    The translation is *primary* data for the AI-screen endpoint (its reason to exist), so unlike
    the enrichment ports a failure here **propagates** rather than degrading to a neutral result.
    """

    @abstractmethod
    def translate(
        self,
        query: str,
        *,
        categories: Sequence[str],
    ) -> EtfScreenIntent:
        """Translate ``query`` into an ``EtfScreenIntent`` of search filters.

        ``categories`` are the category slugs currently present in the stored set — the allowed
        vocabulary the translator constrains its category choices to, so the result maps onto
        values the search can actually match (an implementation may also leave the filter unset
        when the request names no category). The intent is *advisory*: the use case still
        normalizes every field through the ordinary search, so an off-vocabulary or nonsensical
        value degrades to "matches nothing", never an error.

        Raises:
            StockDataUnavailable: the upstream translation failed (a model/vendor error). The
                endpoint surfaces it as a 502 — the request couldn't be understood this time,
                distinct from a well-understood request that simply matched no funds.
        """
        raise NotImplementedError
