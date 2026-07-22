from dataclasses import dataclass


@dataclass(frozen=True)
class EtfSyncCounts:
    added: int
    updated: int
