"""Interface Adapters: the SQLAlchemy-backed universe repositories.

Both implement ``repository.py`` against the shared ``stocks`` anchor — the universe has no
table of its own — and are the only layer that touches SQLAlchemy:

- ``SqlUniverseRepository`` (write side): the screen is written straight onto ``stocks``
  (ticker/name/exchange plus the denormalized ``sector``/``industry``/``market_cap``/
  ``screened_at`` columns). Maps ``ScreenedStock`` / ``CompanyClassification`` entities onto
  anchor rows; ``upsert_screen`` (the screen) and ``set_classification`` (the per-ticker
  enrichment) each commit their own write, so a successful — or partial — sync is durable
  independent of the request.
- ``SqlStockSearchRepository`` (read side): the ``GET /stocks/ticker`` search + the
  ``GET /stocks/classifications`` filter menus, reading those same columns back off the
  anchor. Read-only, and scoped to **screened** rows (``market_cap IS NOT NULL``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy import and_, func, nulls_last, or_, select
from sqlalchemy.orm import Session

from app.stocks.entities import StockPerformance
from app.stocks.stocks.models import StockRecord, get_or_create_stock
from app.stocks.universe.entities import (
    AnchorMetrics,
    Classifications,
    CompanyClassification,
    MarketCapTier,
    PeerCompany,
    ScreenedStock,
    SortDirection,
    StockSearchCriteria,
    StockSearchPage,
    StockSearchResult,
    StockSort,
)
from app.stocks.universe.repository import (
    StockSearchRepository,
    UniverseRepository,
    UniverseSyncCounts,
)


class SqlUniverseRepository(UniverseRepository):
    """Writes the universe through a request-scoped session, onto the ``stocks`` anchor.
    ``upsert_screen`` commits its own write so a successful sync is durable independent of
    the surrounding request.
    """

    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the screen stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def upsert_screen(
        self, stocks: tuple[ScreenedStock, ...]
    ) -> UniverseSyncCounts:
        now = self._now()
        added = 0
        updated = 0
        for stock in stocks:
            anchor = get_or_create_stock(self._session, stock.ticker, stock.name)
            # A stock is "added" the first time the screen marks it (screened_at still
            # null) — whether the anchor is brand new or predates the screen; else it's an
            # in-place refresh.
            if anchor.screened_at is None:
                added += 1
            else:
                updated += 1
            # Fill identity facts when missing; never clobber a settled value (the same
            # rule get_or_create_stock applies to the name). country/currency are the row's
            # market — settled once, like the exchange (a listing doesn't change markets).
            if stock.exchange and not anchor.exchange:
                anchor.exchange = stock.exchange
            if stock.sector and not anchor.sector:
                anchor.sector = stock.sector
            if stock.country and not anchor.country:
                anchor.country = stock.country
            if stock.currency and not anchor.currency:
                anchor.currency = stock.currency
            # Refresh the drifting screen facts + freshness stamp on every run. has_us_listing
            # is recomputed each run (the CA pass sets it; the US pass leaves it False), so it's
            # overwritten, not fill-once — a listing is reclassified if it gains/loses a US sibling.
            anchor.market_cap = stock.market_cap
            anchor.has_us_listing = stock.has_us_listing
            anchor.screened_at = now
        self._session.commit()
        return UniverseSyncCounts(added=added, updated=updated)

    def screened_us_tickers(self) -> frozenset[str]:
        # Every screened US listing (market_cap NOT NULL is the screened gate), uppercased for a
        # case-insensitive base-ticker match. Tickers are stored uppercase already; the upper()
        # is belt-and-suspenders.
        rows = (
            self._session.execute(
                select(StockRecord.ticker).where(
                    StockRecord.country == "US",
                    StockRecord.market_cap.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        return frozenset(ticker.upper() for ticker in rows)

    def screened_us_company_names(self) -> frozenset[str]:
        # The raw names of every screened US listing (same country/screened gate as
        # screened_us_tickers, plus name NOT NULL). Returned unnormalized — the use case
        # normalizes both sides for the .NE CDR name match, keeping that domain rule out of SQL.
        rows = (
            self._session.execute(
                select(StockRecord.name).where(
                    StockRecord.country == "US",
                    StockRecord.market_cap.is_not(None),
                    StockRecord.name.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        return frozenset(rows)

    def tickers_missing_classification(self, limit: int) -> tuple[str, ...]:
        # Missing *either* side: a stock is on the work-list until both sector and industry
        # are filled, so a one-sided classification (Yahoo returned only industry, say) gets
        # revisited instead of being stuck with a null sector forever — set_classification is
        # fill-once per side, so a later run completes it.
        #
        # Largest market cap first (ticker as a stable tiebreak) so a capped, rate-limited
        # run spends its scarce successful .info calls on the biggest, most-viewed names —
        # a megacap like NVDA/GOOGL is classified in the first run rather than starved
        # behind thousands of alphabetically-earlier small caps. A non-screened incidental
        # ticker (market_cap NULL) sorts last, after every screened member.
        rows = (
            self._session.execute(
                select(StockRecord.ticker)
                .where(
                    or_(
                        StockRecord.industry.is_(None),
                        StockRecord.sector.is_(None),
                    )
                )
                .order_by(nulls_last(StockRecord.market_cap.desc()), StockRecord.ticker)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return tuple(rows)

    def set_classification(
        self, ticker: str, classification: CompanyClassification
    ) -> None:
        stock = self._session.execute(
            select(StockRecord).where(StockRecord.ticker == ticker)
        ).scalar_one_or_none()
        if stock is None:
            return
        # Fill-once per side: write only what the source supplies and the column still lacks,
        # so a settled value survives and a one-sided classification leaves room for the rest.
        if classification.industry and not stock.industry:
            stock.industry = classification.industry
        if classification.sector and not stock.sector:
            stock.sector = classification.sector
        self._session.commit()

    def set_pe_ratios(self, pe_by_ticker: Mapping[str, float | None]) -> int:
        # Overwrite, not fill-once: the P/E is recomputed from a fresh price each sweep, so a
        # None legitimately clears a stale figure (a trailing loss, or the quarterly cache
        # dropping below four quarters). One commit for the whole batch — the pass values the
        # entire screened set, so per-ticker commits would be needless churn.
        written = 0
        for ticker, pe in pe_by_ticker.items():
            stock = self._session.execute(
                select(StockRecord).where(StockRecord.ticker == ticker)
            ).scalar_one_or_none()
            if stock is None:
                continue
            stock.pe_ratio = pe
            if pe is not None:
                written += 1
        self._session.commit()
        return written

    def fcf_per_share_by_ticker(self) -> Mapping[str, float]:
        # Every anchor row the annual slice has given an fcf_per_share (its newest reported
        # year's figure). `is_not(None)` keeps the divisor clean; the valuation pass looks up
        # by ticker and ignores any extra rows, so no screened gate is needed here.
        rows = self._session.execute(
            select(StockRecord.ticker, StockRecord.fcf_per_share).where(
                StockRecord.fcf_per_share.is_not(None)
            )
        ).all()
        return {ticker: fcf_ps for ticker, fcf_ps in rows}

    def set_fcf_yields(self, fcf_yield_by_ticker: Mapping[str, float | None]) -> int:
        # Overwrite, mirroring set_pe_ratios: the yield is recomputed from a fresh price each
        # sweep, so a None legitimately clears a stale figure (no fcf_per_share, or no price
        # this run). One commit for the whole batch.
        written = 0
        for ticker, fcf_yield in fcf_yield_by_ticker.items():
            stock = self._session.execute(
                select(StockRecord).where(StockRecord.ticker == ticker)
            ).scalar_one_or_none()
            if stock is None:
                continue
            stock.fcf_yield = fcf_yield
            if fcf_yield is not None:
                written += 1
        self._session.commit()
        return written

    def ev_components_by_ticker(
        self,
    ) -> Mapping[str, tuple[float, float | None, float | None]]:
        # Every anchor row the fundamentals slice has given an `ebitda` (the EV numerator's
        # divisor). `is_not(None)` gates on EBITDA; debt/cash ride along and may be null (the
        # valuation pass treats a missing leg as 0). Looked up by ticker in the pass, so no
        # screened gate is needed here.
        rows = self._session.execute(
            select(
                StockRecord.ticker,
                StockRecord.ebitda,
                StockRecord.total_debt,
                StockRecord.cash_and_equivalents,
            ).where(StockRecord.ebitda.is_not(None))
        ).all()
        return {
            ticker: (ebitda, total_debt, cash)
            for ticker, ebitda, total_debt, cash in rows
        }

    def set_ev_ebitda(self, ev_ebitda_by_ticker: Mapping[str, float | None]) -> int:
        # Overwrite, mirroring set_pe_ratios / set_fcf_yields: the multiple is recomputed from a
        # fresh screen-time market cap each sweep, so a None legitimately clears a stale figure
        # (no cached EBITDA, or no price this run). Keeps its sign like the FCF yield. One commit
        # for the whole batch.
        written = 0
        for ticker, ev_ebitda in ev_ebitda_by_ticker.items():
            stock = self._session.execute(
                select(StockRecord).where(StockRecord.ticker == ticker)
            ).scalar_one_or_none()
            if stock is None:
                continue
            stock.ev_to_ebitda = ev_ebitda
            if ev_ebitda is not None:
                written += 1
        self._session.commit()
        return written


# Each domain sort field → the anchor column (or expression) it orders by. The growth figures
# are nullable (the annual slice may not have filled them yet), so whichever is chosen gets
# wrapped in nulls_last below — a missing figure sorts to the bottom in either direction.
# GROWTH / FORWARD_GROWTH are the equal-weight blend of a pair of growth columns (trailing and
# forward respectively); in SQL a NULL on either leg makes the sum NULL, so a stock missing
# *either* figure sorts last (the same nulls-last rule as the single-metric growth sorts) — the
# blend deliberately ranks only stocks with both. The forward figures are more often null (they
# need two upcoming years), so a forward sort surfaces fewer ranked names than a trailing one.
# PE is the stored trailing P/E, also nullable (unset until the sync values it, or a trailing
# loss), so it rides the same nulls-last rule — ascending surfaces the cheapest on earnings.
_SORT_EXPRESSIONS = {
    StockSort.MARKET_CAP: StockRecord.market_cap,
    StockSort.REVENUE_GROWTH: StockRecord.revenue_growth_yoy,
    StockSort.EPS_GROWTH: StockRecord.eps_growth_yoy,
    StockSort.GROWTH: (StockRecord.revenue_growth_yoy + StockRecord.eps_growth_yoy) / 2.0,
    StockSort.FORWARD_REVENUE_GROWTH: StockRecord.forward_revenue_growth_yoy,
    StockSort.FORWARD_EPS_GROWTH: StockRecord.forward_eps_growth_yoy,
    StockSort.FORWARD_GROWTH: (
        StockRecord.forward_revenue_growth_yoy + StockRecord.forward_eps_growth_yoy
    )
    / 2.0,
    StockSort.PE: StockRecord.pe_ratio,
    StockSort.FCF_GROWTH: StockRecord.fcf_growth_yoy,
    StockSort.FCF_YIELD: StockRecord.fcf_yield,
    StockSort.EV_EBITDA: StockRecord.ev_to_ebitda,
}

# Each market-cap tier → its (min_inclusive, max_exclusive) dollar bounds; ``None`` = unbounded
# on that side. Half-open ranges so adjacent tiers meet without overlapping (a stock at exactly
# $200B is MEGA, not LARGE). The screened gate already drops null caps, so a tier filter is just
# the range bounds on top.
_TIER_BOUNDS = {
    MarketCapTier.MEGA: (200e9, None),
    MarketCapTier.LARGE: (10e9, 200e9),
    MarketCapTier.MID: (2e9, 10e9),
    MarketCapTier.SMALL: (250e6, 2e9),
}


def _tier_range(tier: MarketCapTier):
    """One market-cap tier as a SQL predicate over its half-open bounds — ``>= low`` always
    (every tier is bounded below), plus ``< high`` unless the tier is open-ended above (MEGA).
    OR several of these together for a multi-tier filter (see ``_conditions``)."""
    low, high = _TIER_BOUNDS[tier]
    bounds = [StockRecord.market_cap >= low]
    if high is not None:
        bounds.append(StockRecord.market_cap < high)
    return and_(*bounds)


def _tier_for_market_cap(market_cap: float | None) -> MarketCapTier | None:
    """Bucket a dollar market cap into its ``MarketCapTier`` — the in-Python inverse of
    ``_tier_range``, over the same ``_TIER_BOUNDS``. ``None`` for a null cap or one below the
    smallest tier's floor (nothing to compare it as). Keeps the tier ⇄ dollars mapping in the
    adapter, so the entity's cohort logic works on tiers alone."""
    if market_cap is None:
        return None
    for tier, (low, high) in _TIER_BOUNDS.items():
        if market_cap >= low and (high is None or market_cap < high):
            return tier
    return None


def _escape_like(term: str) -> str:
    """Escape the LIKE metacharacters in a user's search term so a literal ``%`` / ``_``
    matches itself instead of acting as a wildcard. Paired with ``escape="\\"`` on the
    ``.ilike`` calls below (backslash is escaped first so it doesn't double-escape the rest)."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _performance(row: StockRecord) -> StockPerformance | None:
    """The row's trailing-window returns as a ``StockPerformance``, or ``None`` when the
    performance sync hasn't reached it — every window null means "not synced" (a blank tile),
    which the heat map serializes as ``null`` rather than an all-null block. A partially-filled
    row (some windows null for want of history) still yields a block, its blanks preserved."""
    windows = (
        row.perf_one_week,
        row.perf_one_month,
        row.perf_three_month,
        row.perf_six_month,
        row.perf_ytd,
        row.perf_one_year,
    )
    if all(window is None for window in windows):
        return None
    return StockPerformance(*windows)


def _to_result(row: StockRecord) -> StockSearchResult:
    """Map an anchor row onto the slice's read entity (no live price — DB facts only)."""
    return StockSearchResult(
        ticker=row.ticker,
        name=row.name,
        sector=row.sector,
        industry=row.industry,
        market_cap=row.market_cap,
        pe_ratio=row.pe_ratio,
        fcf_yield=row.fcf_yield,
        ev_ebitda=row.ev_to_ebitda,
        revenue_growth_yoy=row.revenue_growth_yoy,
        eps_growth_yoy=row.eps_growth_yoy,
        fcf_growth_yoy=row.fcf_growth_yoy,
        forward_revenue_growth_yoy=row.forward_revenue_growth_yoy,
        forward_eps_growth_yoy=row.forward_eps_growth_yoy,
        in_sp500=row.in_sp500,
        in_nasdaq100=row.in_nasdaq100,
        country=row.country,
        currency=row.currency,
        has_us_listing=row.has_us_listing,
        performance=_performance(row),
    )


class SqlStockSearchRepository(StockSearchRepository):
    """Reads the screened universe off the ``stocks`` anchor through a request-scoped session.

    Read-only — the search never writes. Only screened rows (``market_cap IS NOT NULL``) are
    visible, the gate that keeps incidentally-known, ticker-only rows out of the results.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def search(self, criteria: StockSearchCriteria) -> StockSearchPage:
        conditions = self._conditions(criteria)
        # Total match count, before the page window, so the client can render a pager.
        total = self._session.execute(
            select(func.count()).select_from(StockRecord).where(*conditions)
        ).scalar_one()
        rows = (
            self._session.execute(
                select(StockRecord)
                .where(*conditions)
                .order_by(*self._ordering(criteria))
                .limit(criteria.limit)
                .offset(criteria.offset)
            )
            .scalars()
            .all()
        )
        return StockSearchPage(
            results=tuple(_to_result(row) for row in rows),
            total=total,
            limit=criteria.limit,
            offset=criteria.offset,
        )

    @staticmethod
    def _ordering(criteria: StockSearchCriteria) -> list:
        """The ORDER BY terms for a search.

        With no sort chosen (``criteria.sort is None``) the page is ordered by ticker alone — a
        neutral, stable A→Z, the same tiebreak every metric sort already ends on — so an unsorted
        browse still pages deterministically (``direction`` doesn't apply). With a sort, order by
        its column/expression wrapped in ``nulls_last`` (a stock still missing the figure sinks to
        the bottom in either direction), then ticker as the stable tiebreak so offset paging over
        equal values never skips or repeats a row."""
        if criteria.sort is None:
            return [StockRecord.ticker.asc()]
        expression = _SORT_EXPRESSIONS[criteria.sort]
        ordering = (
            expression.desc()
            if criteria.direction is SortDirection.DESC
            else expression.asc()
        )
        return [nulls_last(ordering), StockRecord.ticker.asc()]

    def classifications(self) -> Classifications:
        return Classifications(
            sectors=self._distinct(StockRecord.sector),
            industries=self._distinct(StockRecord.industry),
        )

    # Mid-cap-and-up floor for the benchmark sample: the $1–2B slice (the screen floor is
    # $1B, so "small" in practice) carries the noisiest trailing P/Es and the weakest
    # comparables, so a peer benchmark reads cleaner off MID + LARGE + MEGA. Matches the
    # MarketCapTier MID lower bound (2e9); mega-caps stay in (this is a floor, not a range).
    _BENCHMARK_MIN_MARKET_CAP = 2e9

    def pe_ratios_for_industry(self, industry: str) -> tuple[float, ...]:
        # Positive P/Es only: `pe_ratio > 0` already drops NULLs (in SQL `NULL > 0` is not
        # true) and non-positive figures (a trailing loss the sync stored as None, or a stray
        # <= 0). `pe_ratio` is only ever written on screened rows, so no separate screened
        # gate is needed — a non-null P/E implies a screened member. The market-cap floor
        # keeps the sample to mid-cap-and-up (see `_BENCHMARK_MIN_MARKET_CAP`).
        rows = (
            self._session.execute(
                select(StockRecord.pe_ratio).where(
                    StockRecord.industry == industry,
                    StockRecord.pe_ratio > 0,
                    StockRecord.market_cap >= self._BENCHMARK_MIN_MARKET_CAP,
                )
            )
            .scalars()
            .all()
        )
        return tuple(rows)

    def industry_for_ticker(self, ticker: str) -> str | None:
        # A single-column read on the anchor. `scalar_one_or_none` maps both "no row" and a
        # row with a null industry to None — the caller (the analysis path) treats both the
        # same way: no industry, so no peer benchmark to attach.
        return self._session.execute(
            select(StockRecord.industry).where(StockRecord.ticker == ticker)
        ).scalar_one_or_none()

    def anchor_metrics_for_ticker(self, ticker: str) -> AnchorMetrics:
        # One row read of every anchor-materialized fundamental (annual slice's cash/growth +
        # the fundamentals slice's margins/ratios/per-share inputs + the anchor's market cap and
        # clean name), same null-collapsing as `industry_for_ticker`: no row -> an empty
        # AnchorMetrics (all None), so the analysis's DB-first overlay simply leaves those fields
        # empty until the syncs have reached the stock (no live-vendor fallback).
        row = self._session.execute(
            select(
                StockRecord.fcf_per_share,
                StockRecord.ocf_per_share,
                StockRecord.revenue_growth_yoy,
                StockRecord.eps_growth_yoy,
                StockRecord.fcf_growth_yoy,
                StockRecord.gross_margin,
                StockRecord.operating_margin,
                StockRecord.net_margin,
                StockRecord.return_on_equity,
                StockRecord.current_ratio,
                StockRecord.debt_to_equity,
                StockRecord.beta,
                StockRecord.book_value_per_share,
                StockRecord.sales_per_share,
                StockRecord.dividend_per_share,
                StockRecord.ebitda,
                StockRecord.total_debt,
                StockRecord.cash_and_equivalents,
                StockRecord.shares_outstanding,
                StockRecord.market_cap,
                StockRecord.name,
            ).where(StockRecord.ticker == ticker)
        ).one_or_none()
        if row is None:
            return AnchorMetrics()
        return AnchorMetrics(
            fcf_per_share=row.fcf_per_share,
            ocf_per_share=row.ocf_per_share,
            revenue_growth_yoy=row.revenue_growth_yoy,
            eps_growth_yoy=row.eps_growth_yoy,
            fcf_growth_yoy=row.fcf_growth_yoy,
            gross_margin=row.gross_margin,
            operating_margin=row.operating_margin,
            net_margin=row.net_margin,
            return_on_equity=row.return_on_equity,
            current_ratio=row.current_ratio,
            debt_to_equity=row.debt_to_equity,
            beta=row.beta,
            book_value_per_share=row.book_value_per_share,
            sales_per_share=row.sales_per_share,
            dividend_per_share=row.dividend_per_share,
            ebitda=row.ebitda,
            total_debt=row.total_debt,
            cash_and_equivalents=row.cash_and_equivalents,
            shares_outstanding=row.shares_outstanding,
            market_cap=row.market_cap,
            name=row.name,
        )

    def tier_for_ticker(self, ticker: str) -> MarketCapTier | None:
        # The anchor's cap, bucketed to its tier. Same null-collapsing as
        # `industry_for_ticker`: no row / null cap -> None, so the caller falls back to the
        # whole-industry benchmark rather than a tier-scoped one.
        cap = self._session.execute(
            select(StockRecord.market_cap).where(StockRecord.ticker == ticker)
        ).scalar_one_or_none()
        return _tier_for_market_cap(cap)

    def industry_peers(
        self, industry: str
    ) -> tuple[tuple[float, MarketCapTier], ...]:
        # The tier-tagged sibling of `pe_ratios_for_industry` — same WHERE (positive P/E, the
        # mid-cap-and-up floor), but it also selects the cap so each peer carries its tier.
        # Every row clears the $2B floor, so `_tier_for_market_cap` never returns None here.
        rows = self._session.execute(
            select(StockRecord.pe_ratio, StockRecord.market_cap).where(
                StockRecord.industry == industry,
                StockRecord.pe_ratio > 0,
                StockRecord.market_cap >= self._BENCHMARK_MIN_MARKET_CAP,
            )
        ).all()
        return tuple(
            (pe, _tier_for_market_cap(cap))
            for pe, cap in rows
            if _tier_for_market_cap(cap) is not None
        )

    def peers_for_industry(self, industry: str) -> tuple[PeerCompany, ...]:
        # Every screened row in the industry (market_cap NOT NULL is the screened gate), with the
        # comparison columns straight off the anchor. No P/E or market-cap floor — a comparison
        # table shows every peer (a null metric is a blank cell) and the cohort's *tier* scoping,
        # applied by PeerComparison.build, does the size-narrowing the benchmark's $2B floor did.
        rows = self._session.execute(
            select(
                StockRecord.ticker,
                StockRecord.name,
                StockRecord.market_cap,
                StockRecord.pe_ratio,
                StockRecord.ev_to_ebitda,
                StockRecord.fcf_yield,
                StockRecord.net_margin,
                StockRecord.revenue_growth_yoy,
            ).where(
                StockRecord.industry == industry,
                StockRecord.market_cap.is_not(None),
            )
        ).all()
        return tuple(
            PeerCompany(
                ticker=row.ticker,
                name=row.name,
                market_cap=row.market_cap,
                pe_ratio=row.pe_ratio,
                ev_ebitda=row.ev_to_ebitda,
                fcf_yield=row.fcf_yield,
                net_margin=row.net_margin,
                revenue_growth_yoy=row.revenue_growth_yoy,
                tier=_tier_for_market_cap(row.market_cap),
            )
            for row in rows
        )

    def _conditions(self, criteria: StockSearchCriteria) -> list:
        """The WHERE terms shared by the count and the page query — the screened gate plus
        whichever filters the criteria carries (a term is added only when its field is set)."""
        conditions = [StockRecord.market_cap.is_not(None)]  # screened-only
        if criteria.query:
            like = f"%{_escape_like(criteria.query)}%"
            # Match name OR ticker — so "NV" surfaces Nvidia (by name) and NVDA (by ticker).
            conditions.append(
                or_(
                    StockRecord.name.ilike(like, escape="\\"),
                    StockRecord.ticker.ilike(like, escape="\\"),
                )
            )
        # Multi-select: match ANY of the chosen sectors/industries (an IN set — one term still
        # renders a plain `= :x`, so a single-value filter is unchanged). An empty tuple adds no
        # term (don't filter on that axis).
        if criteria.sectors:
            conditions.append(StockRecord.sector.in_(criteria.sectors))
        if criteria.industries:
            conditions.append(StockRecord.industry.in_(criteria.industries))
        if criteria.countries:
            # Union of the chosen markets (ISO-2). Keeps a market-cap sort within one currency
            # and lets a client show a single-market board.
            conditions.append(StockRecord.country.in_(criteria.countries))
        if not criteria.include_interlisted:
            # Hide a Canadian listing that duplicates a US company (a CDR or a same-ticker
            # dual-listing) — a client sees the US listing, not the Canadian duplicate. US and
            # Canadian-only rows are has_us_listing=False, so they're unaffected.
            conditions.append(StockRecord.has_us_listing.is_(False))
        if criteria.in_sp500 is not None:
            conditions.append(StockRecord.in_sp500 == criteria.in_sp500)
        if criteria.in_nasdaq100 is not None:
            conditions.append(StockRecord.in_nasdaq100 == criteria.in_nasdaq100)
        if criteria.market_cap_tiers:
            # Union of the chosen tiers: OR one half-open range per tier. The tiers are
            # contiguous, so selecting adjacent ones (mid + large) yields their merged span, and
            # non-adjacent ones (mega + small) their disjoint union — every tier has a lower
            # bound, so each range term always carries at least the `>=` leg.
            conditions.append(or_(*(_tier_range(t) for t in criteria.market_cap_tiers)))
        return conditions

    def _distinct(self, column) -> tuple[str, ...]:
        """The distinct non-null values of an anchor column, sorted — a filter menu."""
        rows = (
            self._session.execute(
                select(column).where(column.is_not(None)).distinct().order_by(column)
            )
            .scalars()
            .all()
        )
        return tuple(rows)
