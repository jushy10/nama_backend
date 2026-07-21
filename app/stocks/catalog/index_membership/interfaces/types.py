from dataclasses import dataclass


@dataclass(frozen=True)
class IndexMembershipSyncCounts:
    sp500_marked: int
    sp500_cleared: int
    nasdaq100_marked: int
    nasdaq100_cleared: int
