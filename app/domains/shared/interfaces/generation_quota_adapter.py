from abc import ABC, abstractmethod

from app.domains.shared.exceptions import QuotaExceeded


class GenerationQuotaAdapter(ABC):
    """Per-client daily budget of metered AI generations; a cache hit never consumes."""

    @abstractmethod
    def try_consume(self, client_id: str) -> bool:
        """Spend one generation from today's budget; False when already exhausted."""


def consume_generation_quota(
    quota: GenerationQuotaAdapter | None, client_id: str | None
) -> None:
    """Raises QuotaExceeded when the day's budget is spent. No-op without a quota or
    client id (non-HTTP callers: tests, crons)."""
    if quota is None or client_id is None:
        return
    if not quota.try_consume(client_id):
        raise QuotaExceeded()
