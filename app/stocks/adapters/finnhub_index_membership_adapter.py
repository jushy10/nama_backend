"""Interface Adapter: index membership from Finnhub.

Finnhub's ``/index/constituents`` endpoint returns an index's current members as a list of
tickers. We call it once per tracked index — ``^GSPC`` for the S&P 500, ``^NDX`` for the
Nasdaq-100 — and return the two ticker sets. It's the only module that knows Finnhub backs
index membership; swap it for another ``IndexMembershipSource`` and only this file changes.

This is a **keyed** source (unlike the price/earnings feeds): Finnhub gates index data behind a
paid plan, so the cron wiring requires ``FINNHUB_API_KEY`` (a missing key is a 503 at the
endpoint, not silent degradation). Mirrors the other Finnhub adapters — an ``httpx.Client`` with
the ``token`` query param, upstream failures mapped to our domain error.

Per-index isolation: each index is fetched independently, and a single index's failure
(transport error, non-200 — e.g. a plan that doesn't cover it — or a bad payload) degrades that
index to an empty set rather than sinking the other. ``fetch`` raises ``StockDataUnavailable``
only when **both** come back empty (nothing usable at all); a single degraded index is left for
the use case's plausibility floor to skip, so a bad response never clears a live index.

Tickers are normalized to the anchor's convention (upper-cased, ``.`` → ``-`` so ``BRK.B`` →
``BRK-B``, matching what Yahoo/Alpaca store), so the reconcile lines up with existing ``stocks``
rows.

Docs: https://finnhub.io/docs/api/indices-constituents
"""

import logging

import httpx

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.index_membership.entities import IndexMembershipSnapshot
from app.stocks.index_membership.ports import IndexMembershipSource

logger = logging.getLogger(__name__)

# Finnhub's index symbols for the indices we track.
_SP500_SYMBOL = "^GSPC"
_NASDAQ100_SYMBOL = "^NDX"

# The port's domain error is phrased per-symbol, but membership isn't about one symbol; use a
# sentinel so a source-wide failure reads sensibly ("'*' is unavailable: …").
_INDICES = "*"


class FinnhubIndexMembershipProvider(IndexMembershipSource):
    """Reads S&P 500 + Nasdaq-100 membership from Finnhub's ``/index/constituents`` (keyed)."""

    _DEFAULT_BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=15.0)

    def fetch(self) -> IndexMembershipSnapshot:
        sp500 = self._fetch_index(_SP500_SYMBOL, "sp500")
        nasdaq100 = self._fetch_index(_NASDAQ100_SYMBOL, "nasdaq100")
        if not sp500 and not nasdaq100:
            raise StockDataUnavailable(
                _INDICES, "no index membership could be fetched from Finnhub"
            )
        return IndexMembershipSnapshot(sp500=sp500, nasdaq100=nasdaq100)

    def _fetch_index(self, symbol: str, label: str) -> frozenset[str]:
        """Fetch one index's constituents into a ticker set. Any failure (transport, non-200, or
        a bad/empty payload) degrades to an empty set — logged, not raised — so the other index
        still syncs; ``fetch`` raises only when both come back empty."""
        try:
            resp = self._http.get(
                "/index/constituents",
                params={"symbol": symbol, "token": self._api_key},
            )
        except httpx.HTTPError as exc:
            logger.warning("index-membership: Finnhub request failed for %s: %s", label, exc)
            return frozenset()
        if resp.status_code != 200:
            body = resp.text[:200].strip() or "<empty body>"
            logger.warning(
                "index-membership: Finnhub %s request failed (HTTP %s): %s",
                label,
                resp.status_code,
                body,
            )
            return frozenset()
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("index-membership: Finnhub %s invalid JSON: %s", label, exc)
            return frozenset()
        constituents = payload.get("constituents") if isinstance(payload, dict) else None
        tickers = frozenset(
            ticker
            for ticker in (_normalize(value) for value in (constituents or []))
            if ticker
        )
        if not tickers:
            logger.warning(
                "index-membership: Finnhub returned no constituents for %s", label
            )
        return tickers


def _normalize(value: object) -> str | None:
    """Trim a Finnhub ticker to the anchor's convention (upper-case, ``.`` → ``-``), or ``None``
    to drop it (blank, non-string, or junk)."""
    if not isinstance(value, str):
        return None
    text = value.strip().upper().replace(".", "-")
    if not text or " " in text or len(text) > 16:
        return None
    return text
