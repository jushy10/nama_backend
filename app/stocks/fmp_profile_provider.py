"""Interface Adapter: a company's clean name and business description from FMP.

Market-data feeds (Alpaca) carry the full legal instrument title ("Apple Inc.
Common Stock") and no summary of what the company does. Financial Modeling Prep's
profile endpoint carries both a clean display name ("Apple Inc.") and a business
description, so the stock view's name and description come from here. FMP's
description runs several hundred words, so we condense it to the first couple of
sentences (see ``_summarize``) — the stock view only wants a quick "what is this
company" blurb. We read FMP's "stable" endpoint first and fall back to the older
``/api/v3`` one (some keys are scoped to the legacy API) — the same dual-endpoint
handling the constituents sync uses. This is the only module that knows FMP
profiles exist; swap it and nothing else changes.

Docs: https://site.financialmodelingprep.com/developer/docs (Company Profile)
"""

import re

import httpx

from app.stocks.entities import CompanyProfile
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import CompanyProfileProvider


class FmpProfileProvider(CompanyProfileProvider):
    """Fetches a company's business description from FMP (free API key required)."""

    _DEFAULT_BASE_URL = "https://financialmodelingprep.com"

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=10.0)

    def get_profile(self, symbol: str) -> CompanyProfile:
        payload = self._fetch_profile(symbol)
        # FMP returns a list of profiles; an unknown symbol yields an empty list,
        # which maps cleanly to "no profile" (best-effort enrichment).
        first = payload[0] if isinstance(payload, list) and payload else {}
        if not isinstance(first, dict):
            first = {}
        # ``companyName`` is the clean display name ("Apple Inc.") — the stock
        # view prefers it over the price feed's full legal title. The description
        # runs long, so condense it to a short blurb (see ``_summarize``).
        description = _clean(first.get("description"))
        return CompanyProfile(
            name=_clean(first.get("companyName")),
            description=_summarize(description) if description else None,
        )

    def _fetch_profile(self, symbol: str):
        """Fetch the raw profile list, preferring the stable endpoint and falling
        back to legacy ``/api/v3`` (some keys are scoped to one API). Raises only
        when every endpoint fails the request, so the body is self-explaining."""
        routes = (
            ("/stable/profile", {"symbol": symbol}),
            (f"/api/v3/profile/{symbol}", {}),
        )
        last_error: object = "no attempt made"
        for path, params in routes:
            try:
                resp = self._http.get(path, params={**params, "apikey": self._api_key})
            except httpx.HTTPError as exc:
                last_error = str(exc)
                continue
            if resp.status_code != 200:
                body = resp.text[:200].strip() or "<empty body>"
                last_error = f"HTTP {resp.status_code}: {body}"
                continue
            try:
                return resp.json()
            except ValueError as exc:
                last_error = f"invalid JSON: {exc}"
        raise StockDataUnavailable(symbol, f"profile request failed ({last_error})")


def _clean(value: object) -> str | None:
    """Normalize FMP's description to a non-empty, trimmed string or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


_MAX_SENTENCES = 2
_MAX_CHARS = 300

# Tokens that end in a period mid-sentence — without this guard a description
# beginning "Apple Inc. is..." would be cut down to just "Apple Inc.".
_ABBREVIATIONS = frozenset(
    {
        "inc", "corp", "co", "ltd", "llc", "plc", "lp", "sa", "ag", "nv",
        "us", "uk", "jr", "sr", "st", "mr", "ms", "dr", "no", "vs",
    }
)

# A sentence boundary: . ! or ? then whitespace then the start of the next
# sentence (a capital or digit). Abbreviations that slip through (e.g. "Co. The")
# are filtered out in _summarize.
_SENTENCE_END = re.compile(r"[.!?]\s+(?=[A-Z0-9])")


def _summarize(text: str) -> str:
    """Condense a verbose company description to its first couple of sentences.

    Keeps the first ``_MAX_SENTENCES`` sentences, guarding against company
    suffixes ("Inc.", "Co.", …) being read as sentence ends, and hard-caps the
    length so a single run-on sentence can't blow past ``_MAX_CHARS`` (cut at a
    word boundary with an ellipsis).
    """
    text = " ".join(text.split())  # collapse newlines / runs of whitespace
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        candidate = text[start : match.start() + 1]
        last_word = candidate.rsplit(" ", 1)[-1].rstrip(".!?").lower()
        if last_word in _ABBREVIATIONS:
            continue  # false boundary (e.g. "Inc."): keep reading
        sentences.append(candidate.strip())
        start = match.end()
        if len(sentences) >= _MAX_SENTENCES:
            break
    else:
        tail = text[start:].strip()
        if tail:
            sentences.append(tail)
    summary = " ".join(sentences).strip()
    if len(summary) > _MAX_CHARS:
        summary = summary[:_MAX_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return summary
