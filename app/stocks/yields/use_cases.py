"""Application Business Rules: the Treasury-yields use cases.

Two whole-market reads with no input beyond an optional lookback: the current
par-yield curve and the 2Y/10Y history. Each depends only on its port; the
adapters behind them are keyless and live-per-request (no table, no cron).
"""

from app.stocks.yields.entities import YieldCurve, YieldHistory
from app.stocks.yields.ports import YieldCurveProvider, YieldHistoryProvider


class GetYieldCurve:
    """Use case: the current US Treasury par-yield curve.

    Takes no input — it reports the whole curve. Tenors come back shortest
    maturity first, carrying the derived 2s10s spread and inversion flag.
    """

    def __init__(self, provider: YieldCurveProvider) -> None:
        self._provider = provider

    def execute(self) -> YieldCurve:
        curve = self._provider.get_yield_curve()
        return YieldCurve(
            as_of=curve.as_of,
            tenors=tuple(sorted(curve.tenors, key=lambda t: t.months)),
        )


class GetYieldHistory:
    """Use case: the 2Y and 10Y Treasury yields over a trailing window.

    ``lookback_days`` is clamped to a sane range so a caller can't ask for a
    negative window or a century of data; the default is ~3 years, enough to
    show a full inversion cycle.
    """

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
