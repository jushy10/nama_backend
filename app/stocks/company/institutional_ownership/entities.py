from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# The two kinds of 13F filer we track, tagged onto each holder row so one feed can carry both.
# "institution" = a bank / asset manager / hedge fund (Yahoo's ``institutional_holders``);
# "mutual_fund" = a registered fund (Yahoo's ``mutualfund_holders``).
HOLDER_TYPE_INSTITUTION = "institution"
HOLDER_TYPE_MUTUAL_FUND = "mutual_fund"


def _position_change(magnitude: float | None, pct_change: float | None) -> float | None:
    if magnitude is None or pct_change is None:
        return None
    frac = pct_change / 100.0
    denom = 1.0 + frac
    if abs(denom) < 1e-9:
        return None
    return magnitude * frac / denom


@dataclass(frozen=True)
class InstitutionalHolder:
    holder: str
    holder_type: str
    date_reported: date
    shares: float | None
    value: float | None
    pct_held: float | None
    pct_change: float | None

    @property
    def is_buyer(self) -> bool:
        return self.pct_change is not None and self.pct_change > 0

    @property
    def is_seller(self) -> bool:
        return self.pct_change is not None and self.pct_change < 0

    @property
    def share_change(self) -> float | None:
        return _position_change(self.shares, self.pct_change)

    @property
    def value_change(self) -> float | None:
        return _position_change(self.value, self.pct_change)


@dataclass(frozen=True)
class OwnershipBreakdown:
    institutions_pct_held: float | None
    insiders_pct_held: float | None
    institutions_float_pct_held: float | None
    institutions_count: int | None

    @property
    def is_empty(self) -> bool:
        return (
            self.institutions_pct_held is None
            and self.insiders_pct_held is None
            and self.institutions_float_pct_held is None
            and self.institutions_count is None
        )


@dataclass(frozen=True)
class HolderFlow:
    buyers_count: int
    sellers_count: int
    shares_bought: float
    shares_sold: float
    value_bought: float
    value_sold: float

    @property
    def net_share_change(self) -> float:
        return self.shares_bought - self.shares_sold

    @property
    def net_value_change(self) -> float:
        return self.value_bought - self.value_sold


@dataclass(frozen=True)
class InstitutionalOwnership:
    symbol: str
    breakdown: OwnershipBreakdown | None = None
    holders: tuple[InstitutionalHolder, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.holders

    @property
    def latest_report_date(self) -> date | None:
        if not self.holders:
            return None
        return max(h.date_reported for h in self.holders)

    @property
    def latest_holders(self) -> tuple[InstitutionalHolder, ...]:
        latest = self.latest_report_date
        if latest is None:
            return ()
        return tuple(h for h in self.holders if h.date_reported == latest)

    @property
    def flow(self) -> HolderFlow:
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
