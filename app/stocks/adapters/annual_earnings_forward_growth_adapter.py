"""Interface Adapter: batch forward growth read from the annual-earnings cache.

The growth screener ranks a whole universe by its expected next-fiscal-year
growth. The annual-earnings slice already stores both legs per stock — the
first upcoming year's consensus (``eps_estimate``/``revenue_estimate``) and the
latest reported year's actuals — so this adapter projects those rows into
``ForwardGrowth`` entities rather than maintaining a second copy, the same move
``annual_earnings_estimates_adapter`` makes for the snapshot. Batch by design:
one query for the whole symbol list (``models.years_by_symbols``), because the
screener would otherwise pay a round-trip per constituent.

Deliberately DB-only — it never falls through to Yahoo. A symbol with no stored
upcoming year is simply omitted from the map ("not seeded yet" is not an
error); the annual-earnings read path fills the cache lazily and the
``sync-annual-earnings`` cron (which also seeds constituents) keeps it current.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.stocks.earnings.annual import models
from app.stocks.earnings.annual.models import StockAnnualEarningsRecord
from app.stocks.entities import ForwardGrowth
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import ForwardGrowthProvider


def _to_forward_growth(
    symbol: str, rows: list[StockAnnualEarningsRecord]
) -> ForwardGrowth | None:
    """Project one stock's stored year rows (ascending by fiscal year) into the
    growth legs: FY1 = the first upcoming year, the base = the latest reported one.
    ``None`` when no upcoming year is stored — nothing forward-looking to screen on.
    A missing *reported* base (Yahoo's gated income statement) still yields legs;
    the growth properties just come out ``None`` and the use case leaves the row
    unranked."""
    # eps_actual is the slice's reported/upcoming discriminator (entities.py).
    reported = [r for r in rows if r.eps_actual is not None]
    upcoming = [r for r in rows if r.eps_actual is None]
    if not upcoming:
        return None
    fy1 = upcoming[0]
    base = reported[-1] if reported else None
    return ForwardGrowth(
        symbol=symbol,
        fiscal_year=fy1.fiscal_year,
        prior_fiscal_year=base.fiscal_year if base else None,
        eps_estimate=fy1.eps_estimate,
        eps_actual=base.eps_actual if base else None,
        revenue_estimate=fy1.revenue_estimate,
        revenue_actual=base.revenue_actual if base else None,
    )


class AnnualEarningsForwardGrowthProvider(ForwardGrowthProvider):
    """Projects the stored annual-earnings rows into a per-symbol growth map."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_forward_growth(self, symbols: list[str]) -> dict[str, ForwardGrowth]:
        try:
            pairs = models.years_by_symbols(self._session, symbols)
        except Exception as exc:  # noqa: BLE001 — storage boundary: any failure → domain error
            raise StockDataUnavailable(
                "screener", f"forward-growth read failed ({exc})"
            ) from exc
        rows_by_symbol: dict[str, list[StockAnnualEarningsRecord]] = {}
        for symbol, row in pairs:
            rows_by_symbol.setdefault(symbol, []).append(row)
        growth_by_symbol = {}
        for symbol, rows in rows_by_symbol.items():
            growth = _to_forward_growth(symbol, rows)
            if growth is not None:
                growth_by_symbol[symbol] = growth
        return growth_by_symbol
