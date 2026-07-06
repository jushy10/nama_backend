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

from sqlalchemy import func, nulls_last, or_, select
from sqlalchemy.orm import Session

from app.stocks.stocks.models import StockRecord, get_or_create_stock
from app.stocks.universe.entities import (
    Classifications,
    CompanyClassification,
    MarketCapTier,
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
            # rule get_or_create_stock applies to the name).
            if stock.exchange and not anchor.exchange:
                anchor.exchange = stock.exchange
            if stock.sector and not anchor.sector:
                anchor.sector = stock.sector
            # Refresh the drifting screen facts + freshness stamp on every run.
            anchor.market_cap = stock.market_cap
            anchor.screened_at = now
        self._session.commit()
        return UniverseSyncCounts(added=added, updated=updated)

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


def _escape_like(term: str) -> str:
    """Escape the LIKE metacharacters in a user's search term so a literal ``%`` / ``_``
    matches itself instead of acting as a wildcard. Paired with ``escape="\\"`` on the
    ``.ilike`` calls below (backslash is escaped first so it doesn't double-escape the rest)."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _to_result(row: StockRecord) -> StockSearchResult:
    """Map an anchor row onto the slice's read entity (no live price — DB facts only)."""
    return StockSearchResult(
        ticker=row.ticker,
        name=row.name,
        sector=row.sector,
        industry=row.industry,
        market_cap=row.market_cap,
        pe_ratio=row.pe_ratio,
        revenue_growth_yoy=row.revenue_growth_yoy,
        eps_growth_yoy=row.eps_growth_yoy,
        forward_revenue_growth_yoy=row.forward_revenue_growth_yoy,
        forward_eps_growth_yoy=row.forward_eps_growth_yoy,
        in_sp500=row.in_sp500,
        in_nasdaq100=row.in_nasdaq100,
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
        expression = _SORT_EXPRESSIONS[criteria.sort]
        ordering = (
            expression.desc()
            if criteria.direction is SortDirection.DESC
            else expression.asc()
        )
        rows = (
            self._session.execute(
                select(StockRecord)
                .where(*conditions)
                # nulls_last so a stock still missing the sort figure sinks to the bottom
                # (either direction); ticker as a stable tiebreak so offset paging over equal
                # values never skips or repeats a row.
                .order_by(nulls_last(ordering), StockRecord.ticker.asc())
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

    def classifications(self) -> Classifications:
        return Classifications(
            sectors=self._distinct(StockRecord.sector),
            industries=self._distinct(StockRecord.industry),
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
        if criteria.sector:
            conditions.append(StockRecord.sector == criteria.sector)
        if criteria.industry:
            conditions.append(StockRecord.industry == criteria.industry)
        if criteria.in_sp500 is not None:
            conditions.append(StockRecord.in_sp500 == criteria.in_sp500)
        if criteria.in_nasdaq100 is not None:
            conditions.append(StockRecord.in_nasdaq100 == criteria.in_nasdaq100)
        if criteria.market_cap_tier is not None:
            low, high = _TIER_BOUNDS[criteria.market_cap_tier]
            if low is not None:
                conditions.append(StockRecord.market_cap >= low)
            if high is not None:
                conditions.append(StockRecord.market_cap < high)
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
