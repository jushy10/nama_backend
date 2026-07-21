from app.stocks.yields.entities import YieldCurve, YieldHistory
from app.stocks.yields.ports import YieldCurveProvider, YieldHistoryProvider


class GetYieldCurve:
    def __init__(self, provider: YieldCurveProvider) -> None:
        self._provider = provider

    def execute(self) -> YieldCurve:
        curve = self._provider.get_yield_curve()
        return YieldCurve(
            as_of=curve.as_of,
            tenors=tuple(sorted(curve.tenors, key=lambda t: t.months)),
        )


class GetYieldHistory:
    _DEFAULT_LOOKBACK_DAYS = 365 * 3
    _MAX_LOOKBACK_DAYS = 365 * 30

    def __init__(self, provider: YieldHistoryProvider) -> None:
        self._provider = provider

    def execute(self, lookback_days: int | None = None) -> YieldHistory:
        days = self._DEFAULT_LOOKBACK_DAYS if lookback_days is None else lookback_days
        if days <= 0:
            raise ValueError("lookback_days must be a positive number of days")
        days = min(days, self._MAX_LOOKBACK_DAYS)
        return self._provider.get_yield_history(days)
