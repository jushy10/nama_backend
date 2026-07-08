"""Abstract persistence ports for the ETF slice.

Dependency Inversion for storage: the use cases are handed a repository and never know whether
it's backed by SQLAlchemy or an in-memory fake (tests). The concrete SQLAlchemy implementations
live in ``db_repository.py``.

Three ports, split by capability (the ``CLAUDE.md`` "one port per capability" rule): the write
side ``EtfRepository`` the sync uses (the screen upsert *and* the per-fund profile enrichment), the
read side ``EtfSearchRepository`` the ``GET /stocks/etfs`` search + ``.../categories`` menu use, and
``EtfLookupRepository`` for the two per-ticker reads the detail card needs. All front the slice's
own ``etfs`` table (and its ``etf_sector_weightings`` / ``etf_top_holdings`` children) — unlike the
stock ``universe`` slice, which is table-less and writes onto the shared ``stocks`` anchor; an ETF
is not a company, so it gets its own tables rather than polluting the stock universe.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.stocks.etfs.entities import (
    EtfCategories,
    EtfProfile,
    EtfSearchCriteria,
    EtfSearchPage,
    EtfSearchResult,
    ScreenedEtf,
)


@dataclass(frozen=True)
class EtfSyncCounts:
    """The row-level outcome of one screen upsert: funds newly inserted (``added``) and existing
    rows refreshed in place (``updated``).

    The sync is **additive** — it never removes an ETF. A fund that later drops out of the screen
    (its AUM slips below the floor, say) keeps its last-screened facts rather than being deleted,
    so there is no ``removed`` count. (The set is stable at ~1,000 names, so lingering staleness
    is a minor, accepted trade-off for never wiping the table on a bad screen.)
    """

    added: int
    updated: int


class EtfRepository(ABC):
    """A persistent store for the screened ETF set + each fund's profile, refreshed by the sync —
    the ``etfs`` table and its ``etf_sector_weightings`` / ``etf_top_holdings`` children."""

    @abstractmethod
    def upsert_screen(self, etfs: tuple[ScreenedEtf, ...]) -> EtfSyncCounts:
        """Upsert every screened ETF into the ``etfs`` table and return the per-row counts.

        For each: create the row if absent, fill ticker/name/exchange when missing (never
        clobbering a settled value), and set/refresh the screen figures
        (``net_assets``/``expense_ratio``) plus the last-screen stamp. Leaves the profile columns
        alone — that's ``upsert_profile``'s job. Additive: ETFs absent from the screen are left
        untouched (no delete). Commits its own write.
        """
        raise NotImplementedError

    @abstractmethod
    def profile_refresh_targets(self, limit: int | None) -> tuple[str, ...]:
        """Return the tickers whose profile most needs a refresh — the enrichment pass's work-list
        — up to ``limit`` of them, or **all** of them when ``limit`` is ``None``.

        Every screened fund is a target (profile figures drift, so there's no "done" state).
        Ordered **stalest first** — never-fetched funds (null ``profile_fetched_at``) ahead of any
        stamped fund, then oldest-refresh first, with ``ticker`` as a stable tiebreak — so a capped,
        rate-limited run spends its budget on the funds most out of date and successive capped runs
        round-robin the whole set rather than starving the tail.
        """
        raise NotImplementedError

    @abstractmethod
    def upsert_profile(self, ticker: str, profile: EtfProfile) -> None:
        """Persist ``ticker``'s profile: the scalars onto the ``etfs`` row (``category`` /
        ``fund_family`` / ``dividend_yield`` / ``description`` / ``nav`` / the trailing returns) and
        the two lists into their child tables (``etf_sector_weightings`` / ``etf_top_holdings``),
        stamping ``profile_fetched_at``.

        **Merge-preserving**, so a partial/transient Yahoo response never erases good stored data
        (the same spirit as the earnings slices' merge sync): each scalar is written only when the
        incoming value is non-``None`` (a field the fetch didn't carry leaves the stored one), and a
        child set is replaced (delete-then-insert) only when the fetch returned rows (an empty list
        leaves the stored rows intact). Does **not** touch ``net_assets`` / ``expense_ratio`` — the
        screen owns those. A no-op if the ticker has no ``etfs`` row. Commits its own write, so a
        partial enrichment sweep is durable.
        """
        raise NotImplementedError


class EtfSearchRepository(ABC):
    """A read-only view over the stored ETF set — what the ``GET /stocks/etfs`` search + the
    ``.../categories`` menu read.

    Read-only by design: the search never writes (the sync owns every column it reads), so this
    is a separate, small port the write-side ``EtfRepository`` doesn't share.
    """

    @abstractmethod
    def search(self, criteria: EtfSearchCriteria) -> EtfSearchPage:
        """Return the page of ETFs matching ``criteria`` plus the total match count.

        Applies the filters that are set (free-text substring on name *or* ticker, category
        slug), orders by the requested sort with a stable ``ticker`` tiebreak and nulls last, and
        cuts the ``limit``/``offset`` window. ``total`` is the pre-window count, for the client's
        pager. An empty result is not an error — it's a page with no rows.
        """
        raise NotImplementedError

    @abstractmethod
    def categories(self) -> EtfCategories:
        """Return the distinct category slugs present in the stored ETF set.

        One flat, sorted, de-duplicated list (nulls excluded) — the FE's filter menu, which the
        search then accepts back as its ``category`` filter.
        """
        raise NotImplementedError


class EtfLookupRepository(ABC):
    """A read-only view over a *single* stored fund, keyed by ticker — the seam for the per-ticker
    reads the search surface doesn't cover.

    Split from ``EtfSearchRepository`` (the "one port per capability" rule) so the *ticker* slice
    can depend on just the membership check — its only question is "is this symbol an ETF?" — and
    the ETF-detail read on the full row + stored profile, without pulling in the whole paginated
    search surface. All are backed by the same ``etfs`` table and its children.
    """

    @abstractmethod
    def is_etf(self, ticker: str) -> bool:
        """Whether ``ticker`` (already normalized) is in the stored ETF universe.

        A single indexed existence check against the ``etfs`` table's unique ``ticker`` column —
        cheap enough to run on every ticker-card request to decide the card's ``asset_type``. A
        miss is not an error: an unknown/equity symbol simply returns ``False``.
        """
        raise NotImplementedError

    @abstractmethod
    def get(self, ticker: str) -> EtfSearchResult | None:
        """Return the stored ``etfs``-table facts for ``ticker`` (already normalized), or ``None``
        when the fund isn't in the universe.

        One indexed row read on the unique ``ticker``: the identity facts (name/exchange) plus the
        stored screen figures (net_assets/expense_ratio) and the ``category`` slug — the anchor the
        ETF-detail endpoint reads before layering the live quote and the stored profile. ``None``
        (not an error) is how the endpoint learns a symbol is not an ETF, so it can answer 404.
        """
        raise NotImplementedError

    @abstractmethod
    def get_stored_profile(self, ticker: str) -> EtfProfile:
        """Return ``ticker``'s stored profile — the scalars off the ``etfs`` row plus the sector
        weightings / top holdings from the child tables — as an ``EtfProfile``.

        The detail read's enrichment source (the endpoint is DB-only — no live Yahoo call). A fund
        with a row but no profile yet (the enrichment pass hasn't reached it) yields an empty
        ``EtfProfile`` (all ``None`` / empty lists), never an error, so the card still serves the
        quote + screen facts around it. ``net_assets`` / ``expense_ratio`` are left ``None`` here —
        the detail resolves them from the stored screen facts (``get``), not the profile.
        """
        raise NotImplementedError
