from app.stocks.company.earnings.annual.interfaces import AnnualEarningsRepositoryAdapter
from app.stocks.entities import AnalystEstimates
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.interfaces import AnalystEstimatesAdapter

# A symbol with no stored forward years yields this rather than an error —
# "no data" is not an error for best-effort enrichment.
_EMPTY = AnalystEstimates(
    fiscal_year=None,
    period_end=None,
    eps_avg=None,
    revenue_avg=None,
)


class AnalystEstimatesAdapterImpl(AnalystEstimatesAdapter):
    def __init__(self, repository: AnnualEarningsRepositoryAdapter) -> None:
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
