"""Abstract persistence ports for the ETF slice.

Dependency Inversion for storage: the use cases are handed a repository and never know whether
it's backed by SQLAlchemy or an in-memory fake (tests). The concrete SQLAlchemy implementations
live in ``db_repository.py``.

Two ports, split by capability (the ``CLAUDE.md`` "one port per capability" rule): the write
side ``EtfRepository`` the sync uses (the screen upsert *and* the per-fund category enrichment),
and the read side ``EtfSearchRepository`` the ``GET /stocks/etfs`` search + ``.../categories``
menu use. Both front the slice's own ``etfs`` table — unlike the stock ``universe`` slice, which
is table-less and writes onto the shared ``stocks`` anchor; an ETF is not a company, so it gets
its own table rather than polluting the stock universe.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.stocks.etfs.entities import (
    EtfCategories,
    EtfClassification,
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
    """A persistent store for the screened ETF set, refreshed by the sync — the ``etfs`` table."""

    @abstractmethod
    def upsert_screen(self, etfs: tuple[ScreenedEtf, ...]) -> EtfSyncCounts:
        """Upsert every screened ETF into the ``etfs`` table and return the per-row counts.

        For each: create the row if absent, fill ticker/name/exchange when missing (never
        clobbering a settled value), and set/refresh the screen figures
        (``net_assets``/``expense_ratio``) plus the last-screen stamp. Leaves ``category``
        alone — that's the enrichment pass's column. Additive: ETFs absent from the screen are
        left untouched (no delete). Commits its own write.
        """
        raise NotImplementedError

    @abstractmethod
    def tickers_missing_category(self, limit: int | None) -> tuple[str, ...]:
        """Return the tickers still missing a ``category`` — the enrichment pass's work-list — up
        to ``limit`` of them, or **all** of them when ``limit`` is ``None``.

        Ordered **largest net_assets first** (ticker as a stable tiebreak), so a capped run
        spends its budget classifying the biggest, most-viewed funds before the long tail — the
        per-ticker source is rate-limited, so only so many succeed per run. Deterministic, so
        successive capped runs still sweep the whole set. A fund the source can't categorise (or
        a run that never reaches it under the cap) simply surfaces again next run.
        """
        raise NotImplementedError

    @abstractmethod
    def set_category(self, ticker: str, classification: EtfClassification) -> None:
        """Fill ``ticker``'s ``category`` on the row from ``classification``.

        Fill-once: written only when the source supplies a category and the column is still
        unset, so a settled value is never clobbered. A no-op if the ticker has no row or the
        classification is empty. Commits its own write, so a partial enrichment sweep is durable.
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
    """A read-only view over a *single* stored fund, keyed by ticker — the seam for the two
    per-ticker reads the search surface doesn't cover.

    Split from ``EtfSearchRepository`` (the "one port per capability" rule) so the *ticker* slice
    can depend on just the membership check — its only question is "is this symbol an ETF?" — and
    the ETF-detail read on the full row, without pulling in the whole paginated search surface.
    Both are backed by the same ``etfs`` table and its unique ``ticker`` index.
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
        stored figures (net_assets/expense_ratio) and the ``category`` slug — the anchor the
        ETF-detail endpoint reads before layering the live quote and the best-effort yfinance
        enrichment. ``None`` (not an error) is how the endpoint learns a symbol is not an ETF, so
        it can answer 404.
        """
        raise NotImplementedError
