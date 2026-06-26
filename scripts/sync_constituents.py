"""Sync the stock screener's universe (S&P 500 + Nasdaq-100) into the database.

Fetches the constituents from Financial Modeling Prep (FMP) — one call per index,
each returning symbol + name + sector — and replaces the ``index_constituents``
table with the result. The running app reads that table (it never calls FMP
while serving), so run this whenever the indices reconstitute (~quarterly):

    export FMP_API_KEY=...                          # free key from financialmodelingprep.com
    export DATABASE_URL=postgresql+psycopg://...    # omit for local sqlite:///./nama.db
    alembic upgrade head                            # create the table (once per DB)
    python scripts/sync_constituents.py

Needs the app installed (it writes through the app's SQLAlchemy models), and the
`index_constituents` table to exist (created by `alembic upgrade head`).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from app.db import SessionLocal
from app.stocks.constituents import ConstituentRecord

# FMP's current "stable" endpoints, with the older /api/v3 slugs as a fallback
# (some keys are scoped to the legacy API). Both return the same JSON shape.
_BASE_STABLE = "https://financialmodelingprep.com/stable"
_BASE_LEGACY = "https://financialmodelingprep.com/api/v3"
_ENDPOINTS = {
    # index -> (stable slug, legacy slug, ConstituentRecord membership column)
    "sp500": ("sp500-constituent", "sp500_constituent", "in_sp500"),
    "nasdaq100": ("nasdaq-constituent", "nasdaq_constituent", "in_nasdaq100"),
}

# Fold FMP's non-GICS sector labels onto the 11 GICS sectors so the screener's
# sector filter speaks one vocabulary; GICS-native names pass through unchanged.
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
    """Fetch one index's constituents, preferring the stable endpoint and falling
    back to the legacy one. A non-list payload is FMP signalling an error (bad
    key, plan limit) — surface it rather than wiping the table for nothing."""
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
        last_error = f"unexpected response from {url.split('?')[0]}: {str(data)[:200]}"
    raise SystemExit(f"FMP constituents fetch failed: {last_error}")


def _clean(value) -> str | None:
    text = (value or "").strip()
    return text or None


def build_universe(rows_by_index: dict[str, list[dict]]) -> dict[str, dict]:
    """Merge per-index FMP rows into symbol -> record fields.

    Pure (no network), so the membership/sector merge is testable: a symbol in
    both indices gets both flags set; the first non-empty name/sector wins.
    """
    universe: dict[str, dict] = {}
    for index, rows in rows_by_index.items():
        column = _ENDPOINTS[index][2]
        for row in rows:
            symbol = _clean(row.get("symbol"))
            if symbol is None:
                continue
            entry = universe.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "name": None,
                    "sector": None,
                    "in_sp500": False,
                    "in_nasdaq100": False,
                },
            )
            entry[column] = True
            sector = _clean(row.get("sector"))
            entry["name"] = entry["name"] or _clean(row.get("name"))
            entry["sector"] = entry["sector"] or (
                _TO_GICS.get(sector, sector) if sector else None
            )
    return universe


def main() -> None:
    key = _api_key()
    rows_by_index = {
        index: _fetch_constituents(slug_stable, slug_legacy, key)
        for index, (slug_stable, slug_legacy, _column) in _ENDPOINTS.items()
    }
    universe = build_universe(rows_by_index)

    with SessionLocal() as session:
        # Full replace in one transaction: a delete + reinsert also drops names
        # that have left an index since the last sync.
        session.query(ConstituentRecord).delete()
        session.add_all(
            ConstituentRecord(
                symbol=e["symbol"],
                name=e["name"],
                sector=e["sector"],
                in_sp500=e["in_sp500"],
                in_nasdaq100=e["in_nasdaq100"],
            )
            for e in universe.values()
        )
        session.commit()
        total = session.query(ConstituentRecord).count()
        sp500 = session.query(ConstituentRecord).filter_by(in_sp500=True).count()
        nasdaq100 = session.query(ConstituentRecord).filter_by(in_nasdaq100=True).count()

    print(
        f"Synced {total} constituents "
        f"({sp500} S&P 500, {nasdaq100} Nasdaq-100) -> index_constituents"
    )


if __name__ == "__main__":
    main()
