"""Generate the static index-constituents file the screener reads.

The screener ranks the day's move across a *universe* of stocks and lets the
caller narrow it by index (S&P 500 / Nasdaq-100) and GICS sector. That needs a
symbol -> (name, sector, index memberships) table, which the app's live data
feed (Alpaca) doesn't expose. Rather than call a constituents API on every
request — rarely-changing data that would burn a rate limit and add a failure
mode — we bake the membership into a static JSON checked into the repo (the same
spirit as the hard-coded sector-ETF map in the Alpaca adapter).

This script regenerates that JSON from **Financial Modeling Prep (FMP)** — one
provider with purpose-built index-constituent endpoints that each return the
symbol, company name, and sector for every member in a single call:

  * S&P 500    - /sp500-constituent
  * Nasdaq-100 - /nasdaq-constituent

Set an FMP API key (free tier) and run it whenever the indices reconstitute
(roughly quarterly):

    export FMP_API_KEY=...
    python scripts/build_constituents.py

stdlib only, so it runs without the app's dependencies installed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

# FMP's current "stable" endpoints, with the older /api/v3 slugs as a fallback
# (some keys are scoped to the legacy API). Both return the same JSON shape.
_BASE_STABLE = "https://financialmodelingprep.com/stable"
_BASE_LEGACY = "https://financialmodelingprep.com/api/v3"
_ENDPOINTS = {
    # index -> (stable slug, legacy slug)
    "sp500": ("sp500-constituent", "sp500_constituent"),
    "nasdaq100": ("nasdaq-constituent", "nasdaq_constituent"),
}

# FMP's sector vocabulary varies by endpoint; fold the non-GICS labels back onto
# the 11 GICS sectors so the screener's sector filter speaks one vocabulary.
# GICS-native names pass through unchanged.
_TO_GICS = {
    "Technology": "Information Technology",
    "Financial Services": "Financials",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Basic Materials": "Materials",
    "Healthcare": "Health Care",
    "Telecommunication Services": "Communication Services",
    "Communication": "Communication Services",
}

OUTPUT = Path(__file__).resolve().parents[1] / "app" / "stocks" / "data" / "constituents.json"

_USER_AGENT = "nama-backend-constituents/1.0 (https://namainsights.com)"


def _api_key() -> str:
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise SystemExit(
            "FMP_API_KEY is not set. Get a free key at financialmodelingprep.com, "
            "then `export FMP_API_KEY=...` before running."
        )
    return key


def _fetch_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (trusted host)
        return json.loads(response.read().decode("utf-8"))


def _fetch_constituents(slug_stable: str, slug_legacy: str, key: str) -> list[dict]:
    """Fetch one index's constituents, preferring the stable endpoint and
    falling back to the legacy one. A non-list payload is FMP signalling an
    error (bad key, plan limit) — surface it rather than writing a broken file.
    """
    last_error: object = "no attempt made"
    for url in (
        f"{_BASE_STABLE}/{slug_stable}?apikey={key}",
        f"{_BASE_LEGACY}/{slug_legacy}?apikey={key}",
    ):
        try:
            data = _fetch_json(url)
        except (urllib.error.URLError, ValueError) as exc:
            last_error = exc
            continue
        if isinstance(data, list) and data:
            return data
        # Dict/empty here is usually {"Error Message": "..."} or a plan notice.
        last_error = f"unexpected response from {url.split('?')[0]}: {str(data)[:200]}"
    raise SystemExit(f"FMP constituents fetch failed: {last_error}")


def _clean(value) -> str | None:
    text = (value or "").strip()
    return text or None


def build() -> dict:
    key = _api_key()
    universe: dict[str, dict] = {}

    for index, (slug_stable, slug_legacy) in _ENDPOINTS.items():
        for row in _fetch_constituents(slug_stable, slug_legacy, key):
            symbol = _clean(row.get("symbol"))
            if symbol is None:
                continue
            entry = universe.setdefault(
                symbol, {"symbol": symbol, "name": None, "sector": None, "indices": set()}
            )
            entry["indices"].add(index)
            # First non-empty wins (a symbol can arrive from both indices).
            sector = _clean(row.get("sector"))
            entry["name"] = entry["name"] or _clean(row.get("name"))
            entry["sector"] = entry["sector"] or (
                _TO_GICS.get(sector, sector) if sector else None
            )

    constituents = [
        {
            "symbol": e["symbol"],
            "name": e["name"],
            "sector": e["sector"],
            "indices": sorted(e["indices"]),
        }
        for e in sorted(universe.values(), key=lambda e: e["symbol"])
    ]
    sp500 = sum(1 for c in constituents if "sp500" in c["indices"])
    nasdaq100 = sum(1 for c in constituents if "nasdaq100" in c["indices"])
    return {
        "_note": (
            "Point-in-time index membership + GICS sector for the stock screener. "
            "Regenerate with scripts/build_constituents.py (needs FMP_API_KEY) when "
            "the indices reconstitute (~quarterly)."
        ),
        "_source": "Financial Modeling Prep (/sp500-constituent, /nasdaq-constituent)",
        "counts": {"total": len(constituents), "sp500": sp500, "nasdaq100": nasdaq100},
        "constituents": constituents,
    }


def main() -> None:
    data = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    counts = data["counts"]
    print(
        f"Wrote {counts['total']} constituents "
        f"({counts['sp500']} S&P 500, {counts['nasdaq100']} Nasdaq-100) -> {OUTPUT}"
    )


if __name__ == "__main__":
    main()
