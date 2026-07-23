from abc import ABC, abstractmethod

from app.domains.shared.exceptions import QuotaExceeded


class GenerationQuotaAdapter(ABC):
    """A per-client daily budget of metered AI generations. Consumed only when a
    generation actually runs — a cache hit never touches it."""

    @abstractmethod
    def try_consume(self, client_id: str) -> bool:
        """Spend one generation from the client's budget for today. Returns False
        when the budget is already exhausted (nothing is consumed)."""


def consume_generation_quota(
    quota: GenerationQuotaAdapter | None, client_id: str | None
) -> None:
    """Raises QuotaExceeded when the client's daily budget is spent. A missing quota
    (unwired, e.g. tests/crons) or missing client id is a no-op — the quota is a
    guard on the metered HTTP paths, never a required capability."""
    if quota is None or client_id is None:
        return
    if not quota.try_consume(client_id):
        raise QuotaExceeded()
