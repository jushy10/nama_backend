"""Interface Adapter: index membership from Wikipedia.

Wikipedia publishes each index's current members as an on-wiki table: the S&P 500 at
``List_of_S&P_500_companies`` (the constituents table, ticker column ``Symbol``) and the
Nasdaq-100 at ``Nasdaq-100`` (the "Current components" table, ticker column ``Ticker``). We fetch
each page and parse its roster into a ticker set. It's the only module that knows Wikipedia backs
index membership; swap it for another ``IndexMembershipSource`` and only this file changes.

**Keyless**, unlike the Finnhub source it replaced — Finnhub gates index constituents behind a
paid plan, which returned ``403 "You don't have access to this resource."`` from the deployed
key. Wikipedia welcomes programmatic reads from data-centre IPs, which is why it works from
Fargate where the Yahoo / Nasdaq / ETF-issuer endpoints block us; we send a descriptive
``User-Agent`` as Wikipedia asks (a blank one can be refused).

Parsing is deliberately by **column signature**, not table position: each page also carries a
*changes* table (S&P "Selected changes", Nasdaq "Component changes") whose add/remove columns
could be mistaken for the roster — that confusion is exactly what sank an earlier scrape attempt
(it grabbed the Nasdaq-100 change-log). We read every table on the page, keep the one whose flat
``Symbol`` / ``Ticker`` column yields the most tickers (the ~500 / ~100-row roster dominates any
stray table), and ignore the rest.

Per-index isolation, the same contract as the Finnhub adapter it replaces: each page is fetched
independently, and a single page's failure (transport, non-200, or an unparseable body) degrades
that index to an empty set rather than sinking the other. ``fetch`` raises ``StockDataUnavailable``
only when **both** come back empty; a single degraded index is left for the use case's plausibility
floor to skip, so a bad response never clears a live index.

Tickers are normalized to the anchor's convention (upper-cased, ``.`` → ``-`` so ``BRK.B`` →
``BRK-B``, matching what Yahoo / Alpaca store), so the reconcile lines up with existing ``stocks``
rows. The ``_http`` attribute is the fake seam the offline tests swap.
"""

from __future__ import annotations

import logging
import re
from io import StringIO

import httpx
import pandas

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.index_membership.entities import IndexMembershipSnapshot
from app.stocks.index_membership.ports import IndexMembershipSource

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


class WikipediaIndexMembershipProvider(IndexMembershipSource):
    """Reads S&P 500 + Nasdaq-100 membership from their Wikipedia rosters (keyless)."""

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
        """Fetch one index's page and parse its roster into a ticker set. Any failure (transport,
        non-200, or an unparseable body) degrades to an empty set — logged, not raised — so the
        other index still syncs; ``fetch`` raises only when both come back empty."""
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
    """Return the ticker set from ``html``'s roster table.

    Reads every table on the page and keeps the one whose flat ``Symbol`` / ``Ticker`` column
    yields the most valid tickers — the roster (~500 / ~100 rows) dominates the page's *changes*
    table, whose add/remove columns aren't a plain ``Symbol`` / ``Ticker`` header and so aren't
    considered at all.
    """
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
    """Trim a Wikipedia ticker to the anchor's convention (upper-case, ``.`` → ``-``), or ``None``
    to drop it (blank, non-string, or not a plausible ticker)."""
    if not isinstance(value, str):
        return None
    text = value.strip().upper().replace(".", "-")
    return text if _TICKER_RE.match(text) else None
