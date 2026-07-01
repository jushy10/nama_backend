"""Interface Adapter: forward analyst estimates read from the annual-earnings cache.

The stock snapshot's forward consensus (forward P/E, forward P/S, FY1→FY2 growth) used
to come from a dedicated ``stock_analyst_estimates`` table with its own Yahoo fetch and
cron. But the annual-earnings slice already stores the *same* consensus — its upcoming
years are built from the very ``earnings_estimate``/``revenue_estimate`` frames the
estimates feed read — so this adapter projects the timeline's forward years into an
``AnalystEstimates`` block instead of maintaining a second copy: the first upcoming
year is FY1, the one after FY2.

Deliberately DB-only — it reads the ``AnnualEarningsRepository`` and never falls
through to Yahoo. Estimates are best-effort enrichment on the snapshot, so a symbol
whose timeline hasn't been cached yet (nothing viewed, cron not run) simply yields an
empty (``is_empty``) block; the annual-earnings read path is what fills the cache
lazily, and its cron is what keeps the rows current.
"""

from app.stocks.earnings.annual.repository import AnnualEarningsRepository
from app.stocks.entities import AnalystEstimates
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import AnalystEstimatesProvider

# A symbol with no stored forward years yields this rather than an error —
# "no data" is not an error for best-effort enrichment.
_EMPTY = AnalystEstimates(
    fiscal_year=None,
    period_end=None,
    eps_avg=None,
    revenue_avg=None,
)


class AnnualEarningsEstimatesProvider(AnalystEstimatesProvider):
    """Projects the stored annual-earnings forward years into an estimates block."""

    def __init__(self, repository: AnnualEarningsRepository) -> None:
        self._repository = repository

    def get_estimates(self, symbol: str) -> AnalystEstimates:
        try:
            timeline = self._repository.get(symbol)
        except Exception as exc:  # noqa: BLE001 — storage boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"annual-earnings estimates read failed ({exc})"
            ) from exc
        if timeline is None:
            return _EMPTY

        future = timeline.future  # upcoming years, soonest first
        if not future:
            return _EMPTY
        fy1 = future[0]
        fy2 = future[1] if len(future) > 1 else None
        return AnalystEstimates(
            fiscal_year=fy1.fiscal_year,
            period_end=fy1.period_end,
            eps_avg=fy1.eps_estimate,
            revenue_avg=fy1.revenue_estimate,
            fiscal_year_fy2=fy2.fiscal_year if fy2 else None,
            eps_avg_fy2=fy2.eps_estimate if fy2 else None,
            revenue_avg_fy2=fy2.revenue_estimate if fy2 else None,
        )
