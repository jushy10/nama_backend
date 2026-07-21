from __future__ import annotations

import logging
import re
from io import StringIO

import httpx
import pandas

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.catalog.index_membership.entities import IndexMembershipSnapshot
from app.stocks.catalog.index_membership.interfaces import IndexMembershipAdapter

logger = logging.getLogger(__name__)

# The Wikipedia articles whose rosters we read.
_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_NASDAQ100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

# The ticker column's header differs by page (S&P "Symbol", Nasdaq "Ticker"); accept either and
# keep the table whose such column yields the most tickers.
_TICKER_HEADERS = {"Symbol", "Ticker"}

# Wikipedia asks automated clients to identify themselves; a blank User-Agent can be refused.
_USER_AGENT = "nama-backend/1.0 (index-membership sync; +https://namainsights.com)"

# A normalized US ticker: upper-case letters/digits and the class-share dash, ≤16 chars. Anything
# else (a footnote-laden cell, a stray non-roster row, a blank) is dropped.
_TICKER_RE = re.compile(r"^[A-Z0-9-]{1,16}$")

# The port's domain error is phrased per-symbol, but membership isn't about one symbol; use a
# sentinel so a source-wide failure reads sensibly ("'*' is unavailable: …").
_INDICES = "*"


class IndexMembershipAdapterImpl(IndexMembershipAdapter):
    def __init__(
        self, sp500_url: str = _SP500_URL, nasdaq100_url: str = _NASDAQ100_URL
    ) -> None:
        self._sp500_url = sp500_url
        self._nasdaq100_url = nasdaq100_url
        self._http = httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )

    def fetch(self) -> IndexMembershipSnapshot:
        sp500 = self._fetch_index(self._sp500_url, "sp500")
        nasdaq100 = self._fetch_index(self._nasdaq100_url, "nasdaq100")
        if not sp500 and not nasdaq100:
            raise StockDataUnavailable(
                _INDICES, "no index membership could be fetched from Wikipedia"
            )
        return IndexMembershipSnapshot(sp500=sp500, nasdaq100=nasdaq100)

    def _fetch_index(self, url: str, label: str) -> frozenset[str]:
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as exc:
            logger.warning(
                "index-membership: Wikipedia request failed for %s: %s", label, exc
            )
            return frozenset()
        if resp.status_code != 200:
            logger.warning(
                "index-membership: Wikipedia %s request failed (HTTP %s)",
                label,
                resp.status_code,
            )
            return frozenset()
        try:
            tickers = _extract_tickers(resp.text)
        except Exception as exc:  # a parse failure for one page must not sink the whole sync
            logger.warning(
                "index-membership: could not parse %s constituents: %s", label, exc
            )
            return frozenset()
        if not tickers:
            logger.warning(
                "index-membership: Wikipedia returned no constituents for %s", label
            )
        return tickers


def _extract_tickers(html: str) -> frozenset[str]:
    best: frozenset[str] = frozenset()
    for table in pandas.read_html(StringIO(html), flavor="lxml"):
        for column in table.columns:
            if isinstance(column, str) and column.strip() in _TICKER_HEADERS:
                tickers = frozenset(
                    t for t in (_normalize(v) for v in table[column].tolist()) if t
                )
                if len(tickers) > len(best):
                    best = tickers
                break  # one ticker column per table
    return best


def _normalize(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper().replace(".", "-")
    return text if _TICKER_RE.match(text) else None
