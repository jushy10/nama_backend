"""Entities: a stock's institutional ownership — the "big money" buys and sells.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than reaching into
the shared ``app/stocks/entities.py``, the same convention as the earnings / recommendations /
news / insider-transactions sub-slices). Pure and vendor-agnostic — stdlib only.

Where the insider slice tracks *insiders'* Form 4 trades, this tracks the **institutions** —
the funds, banks, and asset managers that file quarterly 13F holdings. Two shapes:

- **The holders feed** (``InstitutionalHolder``) — the top institutional and mutual-fund holders
  as of a reported 13F quarter, each with the shares/value it holds, the % of the company it owns,
  and the **quarter-over-quarter change in its position** (``pct_change``). That last field is the
  "big buy / big sell" signal: a fund that grew its stake conviction-bought, one that cut it sold.
  The store accumulates a *history* of these snapshots (one per reported quarter), so a holder's
  stake can be tracked over time.
- **The ownership breakdown** (``OwnershipBreakdown``) — the headline "institutions own 62% of the
  float" summary: what fraction of the company is held by institutions vs. insiders, and how many
  institutions hold it.

``InstitutionalOwnership`` bundles both for a symbol, and rolls the **latest** snapshot's holders
into a net buy-vs-sell read (``flow``) — the institutional analogue of the insider slice's
``summary``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# The two kinds of 13F filer we track, tagged onto each holder row so one feed can carry both.
# "institution" = a bank / asset manager / hedge fund (Yahoo's ``institutional_holders``);
# "mutual_fund" = a registered fund (Yahoo's ``mutualfund_holders``).
HOLDER_TYPE_INSTITUTION = "institution"
HOLDER_TYPE_MUTUAL_FUND = "mutual_fund"


def _position_change(magnitude: float | None, pct_change: float | None) -> float | None:
    """The absolute change in a position given its *current* ``magnitude`` (shares or market
    value) and its quarter-over-quarter ``pct_change`` (percent).

    Yahoo reports the position's current size and its QoQ percent change, not the prior size, so
    the absolute delta is ``current - prior`` where ``prior = current / (1 + frac)``:

        delta = magnitude * frac / (1 + frac)   (frac = pct_change / 100)

    Positive for a holder that added, negative for one that trimmed. ``None`` when either input is
    missing or the arithmetic is undefined (a ``-100%`` change would divide by zero — a fully-sold
    position, which Yahoo wouldn't still list anyway)."""
    if magnitude is None or pct_change is None:
        return None
    frac = pct_change / 100.0
    denom = 1.0 + frac
    if abs(denom) < 1e-9:
        return None
    return magnitude * frac / denom


@dataclass(frozen=True)
class InstitutionalHolder:
    """One institutional (or mutual-fund) holder's stake in a stock as of one reported 13F quarter.

    ``holder_type`` is ``"institution"`` / ``"mutual_fund"``; ``date_reported`` is the 13F period
    the snapshot is as of (the identity a refresh keys on, alongside the holder + type). ``shares``
    / ``value`` are the size of the position (value in dollars); ``pct_held`` is the percent of the
    company's shares it owns; ``pct_change`` is the **quarter-over-quarter change in its position**
    (percent) — the buy/sell signal. Every numeric field is best-effort (``None`` when Yahoo omits
    it), so ``share_change`` / ``value_change`` are best-effort too."""

    holder: str
    holder_type: str
    date_reported: date
    shares: float | None
    value: float | None
    pct_held: float | None
    pct_change: float | None

    @property
    def is_buyer(self) -> bool:
        """The holder grew its position this quarter (``pct_change > 0``) — a "big buy"."""
        return self.pct_change is not None and self.pct_change > 0

    @property
    def is_seller(self) -> bool:
        """The holder cut its position this quarter (``pct_change < 0``) — a "big sell"."""
        return self.pct_change is not None and self.pct_change < 0

    @property
    def share_change(self) -> float | None:
        """The absolute change in shares held this quarter (positive = added), or ``None`` when
        the shares or the percent change is unknown. Derived, not stored."""
        return _position_change(self.shares, self.pct_change)

    @property
    def value_change(self) -> float | None:
        """The dollar value of the shares added/removed this quarter, priced at the current
        market value (positive = added), or ``None`` when value or percent change is unknown."""
        return _position_change(self.value, self.pct_change)


@dataclass(frozen=True)
class OwnershipBreakdown:
    """The headline ownership summary: what fraction of the company institutions and insiders hold.

    All percent (``62.3`` = 62.3% of the float held by institutions). ``institutions_count`` is how
    many institutions hold the stock. Every field is best-effort — ``is_empty`` is true when the
    source carried none of them (so the breakdown is dropped rather than served hollow)."""

    institutions_pct_held: float | None
    insiders_pct_held: float | None
    institutions_float_pct_held: float | None
    institutions_count: int | None

    @property
    def is_empty(self) -> bool:
        """True when no field is present — nothing worth surfacing."""
        return (
            self.institutions_pct_held is None
            and self.insiders_pct_held is None
            and self.institutions_float_pct_held is None
            and self.institutions_count is None
        )


@dataclass(frozen=True)
class HolderFlow:
    """A net buy-vs-sell rollup of the holders in the *latest* reported snapshot.

    Counts of holders that added vs. trimmed, and the summed shares and dollar value bought vs.
    sold (both magnitudes positive) — the "are institutions net buying or selling" read, the
    institutional cousin of the insider slice's ``InsiderSummary``. The value/share legs sum only
    the holders whose change is computable (both the size *and* the percent change present)."""

    buyers_count: int
    sellers_count: int
    shares_bought: float
    shares_sold: float
    value_bought: float
    value_sold: float

    @property
    def net_share_change(self) -> float:
        """Net shares accumulated across the snapshot: bought minus sold (positive = net buying)."""
        return self.shares_bought - self.shares_sold

    @property
    def net_value_change(self) -> float:
        """Net dollar flow across the snapshot: value bought minus value sold (positive = net
        buying)."""
        return self.value_bought - self.value_sold


@dataclass(frozen=True)
class InstitutionalOwnership:
    """A stock's institutional ownership — the accumulated holders feed plus the ownership
    breakdown.

    ``holders`` carries every stored snapshot (newest reported quarter first, largest position
    first within a quarter); ``breakdown`` is the current "institutions own X%" summary (``None``
    when the source didn't carry it). Best-effort: a stock the source covers with no institutional
    holders yields an ``is_empty`` ownership, not an error — the same contract the other
    best-effort slices use. ``flow`` rolls the *latest* snapshot into a net buy-vs-sell read."""

    symbol: str
    breakdown: OwnershipBreakdown | None = None
    holders: tuple[InstitutionalHolder, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when no holder is carried (the primary feed). A lone breakdown with no holders is
        treated as empty for caching — the holders feed is the reason the slice exists."""
        return not self.holders

    @property
    def latest_report_date(self) -> date | None:
        """The most recent reported quarter among the holders, or ``None`` when there are none."""
        if not self.holders:
            return None
        return max(h.date_reported for h in self.holders)

    @property
    def latest_holders(self) -> tuple[InstitutionalHolder, ...]:
        """Only the holders from the most recently reported quarter — the current snapshot the
        ``flow`` summarizes (so a multi-quarter history isn't double-counted)."""
        latest = self.latest_report_date
        if latest is None:
            return ()
        return tuple(h for h in self.holders if h.date_reported == latest)

    @property
    def flow(self) -> HolderFlow:
        """The net buy-vs-sell rollup of the latest snapshot's holders (see ``HolderFlow``)."""
        buyers = sellers = 0
        shares_bought = shares_sold = 0.0
        value_bought = value_sold = 0.0
        for holder in self.latest_holders:
            if holder.is_buyer:
                buyers += 1
                if holder.share_change:
                    shares_bought += holder.share_change
                if holder.value_change:
                    value_bought += holder.value_change
            elif holder.is_seller:
                sellers += 1
                # A seller's change is negative; accumulate the positive magnitude.
                if holder.share_change:
                    shares_sold += -holder.share_change
                if holder.value_change:
                    value_sold += -holder.value_change
        return HolderFlow(
            buyers_count=buyers,
            sellers_count=sellers,
            shares_bought=shares_bought,
            shares_sold=shares_sold,
            value_bought=value_bought,
            value_sold=value_sold,
        )
