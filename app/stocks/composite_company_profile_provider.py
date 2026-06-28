"""Interface Adapter: a company profile merged from two sources.

The clean display name and the business description come from different vendors
on their respective free tiers — Finnhub carries the name, FMP the description —
so this merges them behind the single ``CompanyProfileProvider`` port the use
case depends on (the same "wrap providers of a port, expose the same port" shape
as the caching decorator). Each side is independent and best-effort: a failure
or a miss in one source leaves the other's field intact rather than sinking the
whole profile, and either source may be absent (its key unconfigured).
"""

from app.stocks.entities import CompanyProfile
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import CompanyProfileProvider


class CompositeCompanyProfileProvider(CompanyProfileProvider):
    """Takes the name from one provider and the description from another."""

    def __init__(
        self,
        name_source: CompanyProfileProvider | None = None,
        description_source: CompanyProfileProvider | None = None,
    ) -> None:
        self._name_source = name_source
        self._description_source = description_source

    def get_profile(self, symbol: str) -> CompanyProfile:
        name_profile = self._safe(self._name_source, symbol)
        description_profile = self._safe(self._description_source, symbol)
        return CompanyProfile(
            name=name_profile.name if name_profile else None,
            description=(
                description_profile.description if description_profile else None
            ),
        )

    @staticmethod
    def _safe(
        source: CompanyProfileProvider | None, symbol: str
    ) -> CompanyProfile | None:
        # Isolate each source: an absent source, or one vendor's failure, must
        # not drop the other's field. (Each source is independently cached, so a
        # transient failure here isn't pinned — it retries on the next request.)
        if source is None:
            return None
        try:
            return source.get_profile(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None
