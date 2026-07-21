from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

# The normalized transaction vocabulary the adapter maps both feeds onto.
PURCHASE = "Purchase"
SALE = "Sale"
EXCHANGE = "Exchange"
OTHER = "Other"

# Pulls the dollar figures out of a disclosed amount band ("$1,001 - $15,000" -> 1001, 15000).
_AMOUNT_RE = re.compile(r"\$?\s*([\d,]+)")


def parse_amount_range(amount_range: str | None) -> tuple[float | None, float | None]:
    if not amount_range:
        return (None, None)
    figures: list[float] = []
    for match in _AMOUNT_RE.findall(amount_range):
        try:
            figures.append(float(match.replace(",", "")))
        except ValueError:
            continue
    if not figures:
        return (None, None)
    if len(figures) == 1:
        return (figures[0], None)
    return (figures[0], figures[1])


@dataclass(frozen=True)
class CongressTrade:
    member: str
    chamber: str
    party: str | None
    ticker: str
    company_name: str | None
    tx_type: str
    amount_range: str | None
    transaction_date: date | None
    disclosure_date: date | None
    owner: str | None
    source_url: str | None

    @property
    def is_buy(self) -> bool:
        return self.tx_type == PURCHASE

    @property
    def is_sell(self) -> bool:
        return self.tx_type == SALE

    @property
    def amount_low(self) -> float | None:
        return parse_amount_range(self.amount_range)[0]

    @property
    def amount_high(self) -> float | None:
        return parse_amount_range(self.amount_range)[1]

    @property
    def amount_midpoint(self) -> float | None:
        low, high = parse_amount_range(self.amount_range)
        if low is None:
            return None
        if high is None:
            return low
        return (low + high) / 2

    @property
    def activity_date(self) -> date | None:
        return self.disclosure_date or self.transaction_date


@dataclass(frozen=True)
class CongressSummary:
    buy_count: int
    sell_count: int
    buy_value: float
    sell_value: float

    @property
    def net_value(self) -> float:
        return self.buy_value - self.sell_value


def summarize(trades: tuple[CongressTrade, ...]) -> CongressSummary:
    buy_count = sell_count = 0
    buy_value = sell_value = 0.0
    for trade in trades:
        if trade.is_buy:
            buy_count += 1
            if trade.amount_midpoint is not None:
                buy_value += trade.amount_midpoint
        elif trade.is_sell:
            sell_count += 1
            if trade.amount_midpoint is not None:
                sell_value += trade.amount_midpoint
    return CongressSummary(
        buy_count=buy_count,
        sell_count=sell_count,
        buy_value=buy_value,
        sell_value=sell_value,
    )


@dataclass(frozen=True)
class CongressActivity:
    symbol: str
    trades: tuple[CongressTrade, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.trades

    @property
    def summary(self) -> CongressSummary:
        return summarize(self.trades)


@dataclass(frozen=True)
class CongressMarketActivity:
    trades: tuple[CongressTrade, ...]
    total: int
    window_days: int | None

    @property
    def is_empty(self) -> bool:
        return not self.trades

    @property
    def summary(self) -> CongressSummary:
        return summarize(self.trades)


# The three ways the leaderboard ranks stocks by how much Congressional *attention* they're getting
# over a window. ``members`` (the default) is the breadth of interest — how many distinct members
# touched the stock — which reads as the truest "attention" signal; ``trades`` is the raw disclosure
# count; ``value`` is the estimated gross dollars moved (summed band midpoints, buys + sells).
CongressMetric = Literal["members", "trades", "value"]


@dataclass(frozen=True)
class CongressLeaderboardEntry:
    ticker: str
    company_name: str | None
    trade_count: int
    member_count: int
    buy_count: int
    sell_count: int
    buy_value: float
    sell_value: float
    last_activity: date | None

    @property
    def net_value(self) -> float:
        return self.buy_value - self.sell_value

    @property
    def total_value(self) -> float:
        return self.buy_value + self.sell_value


@dataclass
class _LeaderboardAccumulator:
    ticker: str
    company_name: str | None
    trade_count: int = 0
    members: set[str] = field(default_factory=set)
    buy_count: int = 0
    sell_count: int = 0
    buy_value: float = 0.0
    sell_value: float = 0.0
    last_activity: date | None = None

    def add(self, trade: CongressTrade) -> None:
        self.trade_count += 1
        self.members.add(trade.member)
        # Backfill a name if the first row for this ticker lacked one (best-effort context).
        if self.company_name is None and trade.company_name:
            self.company_name = trade.company_name
        activity = trade.activity_date
        if activity is not None and (
            self.last_activity is None or activity > self.last_activity
        ):
            self.last_activity = activity
        midpoint = trade.amount_midpoint
        if trade.is_buy:
            self.buy_count += 1
            if midpoint is not None:
                self.buy_value += midpoint
        elif trade.is_sell:
            self.sell_count += 1
            if midpoint is not None:
                self.sell_value += midpoint

    def finish(self) -> CongressLeaderboardEntry:
        return CongressLeaderboardEntry(
            ticker=self.ticker,
            company_name=self.company_name,
            trade_count=self.trade_count,
            member_count=len(self.members),
            buy_count=self.buy_count,
            sell_count=self.sell_count,
            buy_value=self.buy_value,
            sell_value=self.sell_value,
            last_activity=self.last_activity,
        )


# The ranking each metric applies: all descending on the metric (biggest attention first), with the
# other two rollups as tiebreakers and finally the ticker ascending, so the order is deterministic
# across stocks that tie (a live-served and cache-served board match). Negating the numeric keys
# gives descending under a single stable ascending sort.
_SORT_KEYS: dict[str, Callable[[CongressLeaderboardEntry], tuple]] = {
    "members": lambda e: (-e.member_count, -e.trade_count, -e.total_value, e.ticker),
    "trades": lambda e: (-e.trade_count, -e.member_count, -e.total_value, e.ticker),
    "value": lambda e: (-e.total_value, -e.trade_count, -e.member_count, e.ticker),
}


def build_leaderboard(
    trades: Iterable[CongressTrade], *, metric: CongressMetric, limit: int
) -> tuple[CongressLeaderboardEntry, ...]:
    accumulators: dict[str, _LeaderboardAccumulator] = {}
    for trade in trades:
        accumulator = accumulators.get(trade.ticker)
        if accumulator is None:
            accumulator = accumulators[trade.ticker] = _LeaderboardAccumulator(
                ticker=trade.ticker, company_name=trade.company_name
            )
        accumulator.add(trade)
    entries = [accumulator.finish() for accumulator in accumulators.values()]
    entries.sort(key=_SORT_KEYS[metric])
    return tuple(entries[:limit])


@dataclass(frozen=True)
class CongressLeaderboard:
    entries: tuple[CongressLeaderboardEntry, ...]
    metric: str
    window_days: int | None
    total_stocks: int

    @property
    def is_empty(self) -> bool:
        return not self.entries
