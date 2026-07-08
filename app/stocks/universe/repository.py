"""Abstract persistence port for the universe slice.

Dependency Inversion for storage: the sync use case is handed a ``UniverseRepository`` and
never knows whether it's backed by SQLAlchemy or an in-memory fake (tests) — it just calls
``upsert_screen``. The concrete SQLAlchemy implementation lives in ``db_repository.py``.

A *Repository*, not a *Provider*: the universe is a slow-moving set refreshed out of band
(the cron endpoint), not a live feed. It writes the screen straight onto the ``stocks``
anchor (ticker/name/exchange plus the denormalized ``sector``/``market_cap``/``screened_at``
columns) — there is no separate universe table.

Two ports, split by capability (the ``CLAUDE.md`` "one port per capability" rule): the
write side ``UniverseRepository`` the sync uses, and the read side ``StockSearchRepository``
the ``GET /stocks/ticker`` search + ``GET /stocks/classifications`` endpoints use. Kept
separate so the sync's fake never grows search methods and vice versa — they just happen to
front the same ``stocks`` anchor.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

from app.stocks.universe.entities import (
    Classifications,
    CompanyClassification,
    ScreenedStock,
    StockSearchCriteria,
    StockSearchPage,
)


@dataclass(frozen=True)
class UniverseSyncCounts:
    """The row-level outcome of one screen upsert: anchors newly marked as screened members
    (``added``) and existing members refreshed in place (``updated``).

    The sync is **additive** — it never removes a stock. A company that later falls below
    the floor keeps its last-screened facts rather than being deleted, because the
    ``stocks`` row is a shared anchor other slices reference; there is no ``removed`` count.
    ``added`` counts a stock the screen marks as a member for the first time (its
    ``screened_at`` was null — whether the anchor is brand new or was created earlier by
    another feature); ``updated`` counts one already carrying screen facts.
    """

    added: int
    updated: int


class UniverseRepository(ABC):
    """A persistent store for the screened universe, refreshed by the sync — the shared
    ``stocks`` anchor, in practice."""

    @abstractmethod
    def upsert_screen(self, stocks: tuple[ScreenedStock, ...]) -> UniverseSyncCounts:
        """Upsert every screened stock onto the ``stocks`` anchor and return the per-row
        counts.

        For each: create the anchor if absent, fill ticker/name/exchange when missing
        (never clobbering a settled value), and set/refresh the screen facts
        (``market_cap``/``sector``/``screened_at``) — ``sector`` only when supplied, so a
        source that omits it doesn't wipe a known one. Additive: stocks absent from the
        screen are left untouched (no delete). Commits its own write.
        """
        raise NotImplementedError

    @abstractmethod
    def tickers_missing_classification(self, limit: int) -> tuple[str, ...]:
        """Return up to ``limit`` tickers still missing a ``sector`` *or* an ``industry`` —
        the enrichment pass's work-list.

        Either side missing keeps a ticker on the list, so a one-sided classification (the
        source returned only industry, say) is revisited until both are filled rather than
        left half-done — ``set_classification`` is fill-once per side, so a later run
        completes it.

        Ordered **largest market cap first** (ticker as a stable tiebreak), so a capped run
        spends its budget on the biggest, most-viewed names before the long tail — a megacap
        is classified in an early run rather than starved behind thousands of smaller,
        alphabetically-earlier ones (which matters because the per-ticker source is
        rate-limited, so only so many succeed per run). Deterministic, so successive capped
        runs still sweep the whole set. A ticker keeps reappearing until it's fully
        classified; a symbol the source can't classify (or a run that never reaches it under
        the cap) simply surfaces again next run. Spans the whole ``stocks`` table, not only
        screened members, so an incidentally-known ticker (no market cap → sorted last) gets
        classified too.
        """
        raise NotImplementedError

    @abstractmethod
    def set_classification(
        self, ticker: str, classification: CompanyClassification
    ) -> None:
        """Fill ``ticker``'s ``sector`` / ``industry`` on the anchor from ``classification``.

        Fill-once, like the other anchor facts: a side is written only when the source
        supplies it and the column is still unset, so a settled value is never clobbered and
        a half classification (only one side known) leaves room for the other later. A no-op
        if the ticker has no row. Commits its own write, so a partial enrichment sweep is
        durable.
        """
        raise NotImplementedError

    @abstractmethod
    def set_pe_ratios(self, pe_by_ticker: Mapping[str, float | None]) -> int:
        """Overwrite each ticker's trailing ``pe_ratio`` on the anchor in one commit, and
        return how many were written with a non-null value.

        The valuation counterpart of the screen/classification writes. Unlike the fill-once
        ``set_classification`` this **overwrites** (like ``market_cap`` and the trailing-growth
        pair), because the P/E is recomputed from a fresh price every sweep. A ``None`` value
        clears a prior figure — the trailing year turned a loss, or the quarterly cache fell
        below four quarters. A ticker with no anchor row is skipped. Commits once, so the
        valuation pass is durable independent of the request.
        """
        raise NotImplementedError


class StockSearchRepository(ABC):
    """A read-only view over the screened universe on the ``stocks`` anchor — what the
    ``GET /stocks/ticker`` search and ``GET /stocks/classifications`` endpoints read.

    Read-only by design: the search never writes (the sync owns every column it reads), so
    this is a separate, small port the write-side ``UniverseRepository`` doesn't share.
    """

    @abstractmethod
    def search(self, criteria: StockSearchCriteria) -> StockSearchPage:
        """Return the page of screened stocks matching ``criteria`` plus the total match count.

        Only **screened** rows are searchable (``market_cap IS NOT NULL``) — the gate that
        tells a curated company apart from a symbol the app merely knows incidentally (a
        ticker-card lookup that left name/cap/sector null). Applies the filters that are set
        (free-text substring on name *or* ticker, sector/industry slug, the two index flags),
        orders by the requested sort with a stable ``ticker`` tiebreak and nulls last, and cuts
        the ``limit``/``offset`` window. ``total`` is the pre-window count, for the client's
        pager. An empty result is not an error — it's a page with no rows.
        """
        raise NotImplementedError

    @abstractmethod
    def classifications(self) -> Classifications:
        """Return the distinct sector and industry slugs present in the universe.

        Two flat, sorted, de-duplicated lists (nulls excluded) — the FE's filter menus, which
        the search then accepts back as its ``sector`` / ``industry`` filters.
        """
        raise NotImplementedError

    @abstractmethod
    def pe_ratios_for_industry(self, industry: str) -> tuple[float, ...]:
        """Return the *positive* trailing P/Es of the screened stocks in ``industry`` — the
        sample a peer-valuation benchmark is built from.

        Only usable multiples: null and non-positive P/Es are excluded (a P/E off a loss is
        meaningless), so the caller gets a clean list to summarize. ``industry`` is a stored
        slug; an unknown one (or one with no valued members yet) yields an empty tuple — no
        coverage, not an error.
        """
        raise NotImplementedError
