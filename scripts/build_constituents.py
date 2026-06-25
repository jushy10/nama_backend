"""Generate the static index-constituents file the screener reads.

The screener ranks the day's move across a *universe* of stocks and lets the
caller narrow it by index (S&P 500 / Nasdaq-100) and GICS sector. That needs a
symbol -> (name, sector, index memberships) table, which no market-data feed in
this app provides. Rather than depend on a paid constituents API at request
time, we bake the membership into a static JSON checked into the repo (the same
spirit as the hard-coded sector-ETF map in the Alpaca adapter).

This script regenerates that JSON from two public sources:

  * S&P 500   - the `datasets/s-and-p-500-companies` CSV (Symbol, Security,
                GICS Sector). Authoritative GICS sector per name.
  * Nasdaq-100 - the Wikipedia "Nasdaq-100" constituents table (Ticker,
                Company, ICB Industry).

Sector taxonomy is kept consistent on GICS: a Nasdaq-100 name that is also in
the S&P 500 (the large majority) takes its GICS sector from the CSV; the few
Nasdaq-only names fall back to an ICB->GICS mapping.

Run it whenever the indices reconstitute (roughly quarterly):

    python scripts/build_constituents.py

stdlib only, so it runs without the app's dependencies installed.
"""

from __future__ import annotations

import csv
import io
import json
import re
import urllib.request
from pathlib import Path

SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/"
    "s-and-p-500-companies/main/data/constituents.csv"
)
NASDAQ100_WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100?action=raw"

# Wikipedia's table is keyed on ICB industries; map the ones that differ in
# name onto their GICS-sector equivalent so the whole file speaks one taxonomy.
# (Identically-named industries pass through unchanged.)
_ICB_TO_GICS = {
    "Technology": "Information Technology",
    "Telecommunications": "Communication Services",
    "Basic Materials": "Materials",
}

OUTPUT = Path(__file__).resolve().parents[1] / "app" / "stocks" / "data" / "constituents.json"

_USER_AGENT = "nama-backend-constituents/1.0 (https://namainsights.com)"


def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (trusted URLs)
        return response.read().decode("utf-8")


def _strip_wiki_markup(cell: str) -> str:
    """Turn a wikitext cell into plain text.

    `[[Adobe Inc.]]` -> "Adobe Inc."; `[[AMD|Advanced Micro Devices]]` ->
    "Advanced Micro Devices" (the display text after the pipe); trailing text
    such as "(Class A)" is preserved.
    """
    text = cell.strip().strip("|").strip()
    text = re.sub(r"\[\[([^\]]+)\]\]", lambda m: m.group(1).split("|")[-1], text)
    return re.sub(r"<[^>]+>", "", text).strip()  # drop any stray <ref> tags


def _load_sp500() -> dict[str, dict]:
    """Symbol -> {name, sector, indices} for every S&P 500 constituent."""
    rows = csv.DictReader(io.StringIO(_fetch(SP500_CSV_URL)))
    out: dict[str, dict] = {}
    for row in rows:
        symbol = (row.get("Symbol") or "").strip()
        if not symbol:
            continue
        out[symbol] = {
            "symbol": symbol,
            "name": (row.get("Security") or "").strip() or None,
            "sector": (row.get("GICS Sector") or "").strip() or None,
            "indices": {"sp500"},
        }
    return out


def _parse_nasdaq100(wikitext: str) -> list[tuple[str, str, str]]:
    """Extract (ticker, company, icb_industry) rows from the constituents table."""
    start = wikitext.index('id="constituents"')
    table = wikitext[start : wikitext.index("\n|}", start)]
    rows: list[tuple[str, str, str]] = []
    for line in table.splitlines():
        line = line.rstrip()
        if not line.startswith("|") or line.startswith(("|-", "|}")):
            continue  # separator / closer, not a data row
        cells = line.split("||")
        if len(cells) < 2:
            continue
        ticker = _strip_wiki_markup(cells[0]).upper()
        company = _strip_wiki_markup(cells[1])
        icb = _strip_wiki_markup(cells[2]) if len(cells) > 2 else ""
        if ticker:
            rows.append((ticker, company, icb))
    return rows


def build() -> dict:
    universe = _load_sp500()

    nasdaq = _parse_nasdaq100(_fetch(NASDAQ100_WIKI_URL))
    for ticker, company, icb in nasdaq:
        entry = universe.get(ticker)
        if entry is not None:
            entry["indices"].add("nasdaq100")  # already an S&P 500 name
            continue
        universe[ticker] = {
            "symbol": ticker,
            "name": company or None,
            "sector": _ICB_TO_GICS.get(icb, icb) or None,
            "indices": {"nasdaq100"},
        }

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
            "Regenerate with scripts/build_constituents.py when the indices "
            "reconstitute (~quarterly)."
        ),
        "_sources": [SP500_CSV_URL, NASDAQ100_WIKI_URL],
        "counts": {
            "total": len(constituents),
            "sp500": sp500,
            "nasdaq100": nasdaq100,
        },
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
