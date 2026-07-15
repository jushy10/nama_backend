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
    AnchorMetrics,
    Classifications,
    CompanyClassification,
    MarketCapTier,
    PeerCompany,
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

        For each: create the anchor if absent, fill ticker/name/exchange/country/currency
        when missing (never clobbering a settled value — country/currency are fill-once
        market facts, like the exchange), and set/refresh the screen facts
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

    @abstractmethod
    def fcf_per_share_by_ticker(self) -> Mapping[str, float]:
        """Return ``{ticker: fcf_per_share}`` for every anchor row carrying a non-null
        ``fcf_per_share`` — the annual slice's stored newest-reported-year figure.

        The divisor the valuation pass pairs with the screen-time price to materialize
        ``fcf_yield``, the exact analogue of the quarterly TTM the P/E pass reads. A read (not
        a write), but it lives on the write-side repository because it feeds the sync's
        valuation pass; a ticker the annual slice hasn't reached is simply absent, so its
        ``fcf_yield`` stays null.
        """
        raise NotImplementedError

    @abstractmethod
    def set_fcf_yields(self, fcf_yield_by_ticker: Mapping[str, float | None]) -> int:
        """Overwrite each ticker's materialized ``fcf_yield`` on the anchor in one commit, and
        return how many were written with a non-null value.

        The FCF sibling of :meth:`set_pe_ratios` — the screen-time price over the stored
        ``fcf_per_share``, as a signed percent, so the search list is sortable by cash
        cheapness. Overwrites (a price-derived snapshot), and a ``None`` clears a prior figure
        (the annual slice dropped ``fcf_per_share``, or there was no price this sweep). Unlike
        the P/E it keeps its sign — a cash-burner reads negative. A ticker with no anchor row
        is skipped. Commits once.
        """
        raise NotImplementedError

    @abstractmethod
    def ev_components_by_ticker(self) -> Mapping[str, tuple[float, float | None, float | None]]:
        """Return ``{ticker: (ebitda, total_debt, cash_and_equivalents)}`` for every anchor row
        carrying a non-null ``ebitda`` — the fundamentals slice's stored enterprise-value inputs.

        The pieces the valuation pass pairs with the screen-time ``market_cap`` to materialize
        ``ev_to_ebitda`` (enterprise value = market cap + debt − cash, over EBITDA), the EV
        analogue of :meth:`fcf_per_share_by_ticker`. Only ``ebitda`` is required (its non-null
        row is the gate); ``total_debt`` / ``cash_and_equivalents`` ride along and may be
        ``None`` (the pass treats a missing leg as ``0``). A read on the write-side repository
        because it feeds the sync; a ticker the fundamentals slice hasn't reached is simply
        absent, so its ``ev_to_ebitda`` stays null.
        """
        raise NotImplementedError

    @abstractmethod
    def set_ev_ebitda(self, ev_ebitda_by_ticker: Mapping[str, float | None]) -> int:
        """Overwrite each ticker's materialized ``ev_to_ebitda`` on the anchor in one commit,
        and return how many were written with a non-null value.

        The EV sibling of :meth:`set_pe_ratios` / :meth:`set_fcf_yields` — enterprise value at
        the screen-time market cap over trailing EBITDA, so the search list and the peer
        comparison are sortable/readable off one DB query. Overwrites (a price-derived
        snapshot), and a ``None`` clears a prior figure (no EBITDA cached, or no price this
        sweep). Like the card's live figure it keeps its sign — a net-cash company below its
        cash reads negative. A ticker with no anchor row is skipped. Commits once.
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
        """Return the *positive* trailing P/Es of the **mid-cap-and-up** screened stocks in
        ``industry`` — the sample a peer-valuation benchmark is built from.

        Only usable multiples: null and non-positive P/Es are excluded (a P/E off a loss is
        meaningless), and the $1–2B tail is dropped (a market-cap floor of $2B), so the
        caller gets a clean list of comparable names to summarize. ``industry`` is a stored
        slug; an unknown one (or one with no valued members yet) yields an empty tuple — no
        coverage, not an error.
        """
        raise NotImplementedError

    @abstractmethod
    def industry_for_ticker(self, ticker: str) -> str | None:
        """Return the stored industry slug for ``ticker``, or ``None`` when the anchor has no
        row for it or the industry hasn't been classified yet.

        The primitive that lets a *ticker-driven* caller (the AI analysis) find the industry
        whose P/E benchmark to build via :meth:`pe_ratios_for_industry` — resolving the
        ticker's own industry and summarizing its peers is the use case's job, so this stays a
        single-column read on the same taxonomy the search filters and the benchmark group on.
        """
        raise NotImplementedError

    @abstractmethod
    def anchor_metrics_for_ticker(self, ticker: str) -> AnchorMetrics:
        """Return the anchor figures the annual-earnings slice materializes for ``ticker`` —
        the trailing free-cash-flow per share and the trailing revenue/EPS growth — as an
        :class:`AnchorMetrics`, all fields ``None`` when the anchor has no row or hasn't been
        given them yet.

        The AI analysis reads these from **here**, the same DB-materialized figures the ticker
        card and universe search use, rather than the live fundamentals vendor — so the
        scorecard's Cash Generation and Growth sections never diverge from the rest of the app
        (the vendor's cash-flow/growth figures sit on a different basis). One multi-column
        anchor read, the ticker-driven sibling of :meth:`industry_for_ticker`.
        """
        raise NotImplementedError

    @abstractmethod
    def tier_for_ticker(self, ticker: str) -> MarketCapTier | None:
        """Return the market-cap size tier of ``ticker``, or ``None`` when its cap is unknown.

        The companion to :meth:`industry_for_ticker` for a *tier-anchored* peer comparison —
        it lets a ticker-driven caller scope the benchmark to the stock's own size class
        (a mega-cap against other mega-caps). Bucketing dollars into a tier is the adapter's
        job (the same tier ⇄ bounds mapping the ``market_cap`` search filter uses), so the
        use case gets a tier back, never a dollar figure.
        """
        raise NotImplementedError

    @abstractmethod
    def industry_peers(
        self, industry: str
    ) -> tuple[tuple[float, MarketCapTier], ...]:
        """Return the mid-cap-and-up peers of ``industry`` as ``(positive_pe, tier)`` pairs.

        The tier-tagged form of :meth:`pe_ratios_for_industry` — same sample (positive P/Es,
        the $2B floor), but each peer carries its size tier so a caller can build a
        tier-scoped cohort (see :meth:`IndustryValuation.for_stock_peers`). An unknown
        industry (or one with no valued members) yields an empty tuple — no coverage, not an
        error.
        """
        raise NotImplementedError

    @abstractmethod
    def peers_for_industry(self, industry: str) -> tuple[PeerCompany, ...]:
        """Return **every** screened company in ``industry`` as a :class:`PeerCompany` row — the
        candidate set the peer comparison scopes and medians.

        The named-row sibling of :meth:`industry_peers` (which returns only ``(pe, tier)`` pairs
        for the aggregate benchmark): each row carries the anchor's comparison columns
        (market cap, the P/E and EV/EBITDA snapshots, FCF yield, net margin, trailing revenue
        growth) and its size ``tier``. Unlike the benchmark sample there is **no P/E floor and no
        $2B floor** — a comparison table shows every peer (a missing metric is a blank cell, and
        the tier scoping, not a market-cap floor, keeps the cohort sensible), and it includes the
        looked-up stock itself when it's a screened member of the industry. Screened rows only
        (``market_cap IS NOT NULL``). An unknown industry yields an empty tuple — no coverage,
        not an error.
        """
        raise NotImplementedError
