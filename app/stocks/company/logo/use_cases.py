import re

from app.stocks.company.logo.entities import Logo
from app.stocks.company.logo.ports import LogoProvider

# A ticker: 1-5 letters, optionally an exchange or share-class suffix — a dot then 1-3 letters.
# The suffix is what lets a Canadian listing get its *own* logo: Logo.dev is exchange-aware, so
# keeping the suffix disambiguates a collision (``T.TO`` is Telus, bare ``T`` is AT&T) and returns
# the right image for the TSX / TSXV venues (``.TO`` / ``.V`` / ``.NE`` / ``.CN``) as well as US
# class shares (``BRK.B``). The dot-and-letters shape is also all a raw ticker may contain before
# it's spliced into the vendor URL path, so it doubles as the injection guard. A bare ticker still
# matches (the suffix group is optional), so US symbols are unchanged.
_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z]{1,3})?$")


def _normalize_symbol(symbol: str) -> str:
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not _SYMBOL_RE.match(normalized):
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


class GetStockLogo:
    def __init__(self, provider: LogoProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> Logo:
        return self._provider.get_logo(_normalize_symbol(symbol))
