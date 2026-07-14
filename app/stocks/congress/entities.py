"""Entities: a stock's (and the market's) recent Congressional stock trades.

Slice-local domain objects (this slice keeps its own ``entities`` rather than reaching into the
shared ``app/stocks/entities.py``, the same convention as the earnings / recommendations / news /
insider-transactions sub-slices). Pure and vendor-agnostic — stdlib only.

Members of the US House and Senate must disclose their (and their spouse's / dependents') stock
trades within 45 days under the **STOCK Act**. Two facts about that domain shape everything here:

- **Congress discloses a dollar *range*, never an exact amount** (``"$1,001 - $15,000"``). So the
  precise ``value`` an insider Form 4 carries doesn't exist; the entity keeps the raw
  ``amount_range`` string for display and derives a best-effort ``amount_midpoint`` (the middle of
  the band) purely so a feed can be rolled up or a "largest trade" surfaced — it is an estimate,
  not a reported figure.
- **A filed disclosure is a frozen fact.** Once a member reports a trade it never changes, so the
  store accumulates history and the cache upsert is insert-only (like the insider / rating-changes
  slices), keyed on ``(member, ticker, transaction_date, amount, chamber)``.

``tx_type`` is normalized by the adapter to one small vocabulary — ``"Purchase"`` / ``"Sale"`` /
``"Exchange"`` / ``"Other"`` — because the House and Senate feeds phrase it differently (the Senate
splits a sale into ``"Sale (Full)"`` / ``"Sale (Partial)"``). ``is_buy`` / ``is_sell`` are the
buy/sell signal derived from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# The normalized transaction vocabulary the adapter maps both feeds onto.
PURCHASE = "Purchase"
SALE = "Sale"
EXCHANGE = "Exchange"
OTHER = "Other"

# Pulls the dollar figures out of a disclosed amount band ("$1,001 - $15,000" -> 1001, 15000).
_AMOUNT_RE = re.compile(r"\$?\s*([\d,]+)")


def parse_amount_range(amount_range: str | None) -> tuple[float | None, float | None]:
    """The (low, high) dollar bounds parsed from a disclosed amount band, best-effort.

    Congress reports bands, so ``"$1,001 - $15,000"`` -> ``(1001.0, 15000.0)``. A one-sided or
    malformed value (some feed rows carry only a single figure) yields ``(low, None)``; an absent
    or unparseable amount yields ``(None, None)``. Pure — no I/O, exercised directly by the tests.
    """
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
    """One member's one disclosed trade in a stock.

    ``chamber`` is ``"House"`` or ``"Senate"``; ``party`` is best-effort and usually ``None`` (the
    keyless feeds don't carry it). ``tx_type`` is the normalized action (``PURCHASE`` / ``SALE`` /
    ``EXCHANGE`` / ``OTHER``); ``amount_range`` is the disclosed band verbatim; ``owner`` is who
    holds the position (``"Self"`` / ``"Spouse"`` / ``"Joint"`` / ``"Dependent Child"``).
    ``transaction_date`` is when the trade happened and ``disclosure_date`` when it was reported —
    the two can be weeks apart (the STOCK Act allows 45 days). ``company_name`` and ``source_url``
    are best-effort context. Any of the nullable fields can be ``None`` when the feed omits them.
    """

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
        """A purchase — the member bought the stock."""
        return self.tx_type == PURCHASE

    @property
    def is_sell(self) -> bool:
        """A sale — the member sold the stock (a full or partial sale, both normalized to SALE)."""
        return self.tx_type == SALE

    @property
    def amount_low(self) -> float | None:
        """The low bound of the disclosed dollar band (``None`` when unparseable)."""
        return parse_amount_range(self.amount_range)[0]

    @property
    def amount_high(self) -> float | None:
        """The high bound of the disclosed dollar band (``None`` when the band is one-sided)."""
        return parse_amount_range(self.amount_range)[1]

    @property
    def amount_midpoint(self) -> float | None:
        """The middle of the disclosed band — a best-effort estimate of the trade's size (Congress
        never reports the exact figure), for rolling up a feed or surfacing the biggest trades.
        The low bound when the band is one-sided; ``None`` when nothing parses."""
        low, high = parse_amount_range(self.amount_range)
        if low is None:
            return None
        if high is None:
            return low
        return (low + high) / 2

    @property
    def activity_date(self) -> date | None:
        """The single date this trade is ordered/windowed by: the disclosure date (when it became
        public — the "news" moment a board sorts on) falling back to the transaction date. ``None``
        only when the feed carried neither."""
        return self.disclosure_date or self.transaction_date


@dataclass(frozen=True)
class CongressSummary:
    """A net buy-vs-sell rollup of a set of trades — counts and estimated dollar flow.

    The dollar legs sum each trade's ``amount_midpoint`` (best-effort, since Congress discloses
    only bands), so they're an *estimate* of the money moved, not a reported total. ``net_value``
    is buy minus sell (positive = Congress net buying)."""

    buy_count: int
    sell_count: int
    buy_value: float
    sell_value: float

    @property
    def net_value(self) -> float:
        """Estimated net dollar flow: buy value minus sell value (positive = net buying)."""
        return self.buy_value - self.sell_value


def summarize(trades: tuple[CongressTrade, ...]) -> CongressSummary:
    """Roll a run of trades into a net buy-vs-sell ``CongressSummary`` — the shared reducer the
    per-ticker and market views both use. Only purchases and sales count toward the flow; an
    ``EXCHANGE`` / ``OTHER`` action contributes to neither leg."""
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
    """A single stock's recent Congressional trades — every stored disclosure, newest first.

    Best-effort: a stock Congress hasn't traded (or the cron hasn't seeded yet) yields an empty
    (``is_empty``) activity, not an error — the same contract the other best-effort slices use.
    ``summary`` rolls the trades into a net buy-vs-sell read.
    """

    symbol: str
    trades: tuple[CongressTrade, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when no trade is carried (no Congressional activity on file for this stock)."""
        return not self.trades

    @property
    def summary(self) -> CongressSummary:
        """A net buy-vs-sell rollup of the stored (already-recent, bounded) trades, on access."""
        return summarize(self.trades)


@dataclass(frozen=True)
class CongressMarketActivity:
    """A window of the whole market's recent Congressional trades — the market board's view.

    Spans every stock rather than one, newest first, with the pagination envelope the endpoint
    surfaces. ``total`` is the full match count in the window before the page was cut, so the
    client can size its pager. ``summary`` rolls the *page's* trades into a net read.
    """

    trades: tuple[CongressTrade, ...]
    total: int
    window_days: int | None

    @property
    def is_empty(self) -> bool:
        return not self.trades

    @property
    def summary(self) -> CongressSummary:
        return summarize(self.trades)
