"""Interface Adapter: the US market screen from Nasdaq.

Nasdaq's public screener (``/api/screener/stocks``) returns the whole US board — NASDAQ,
NYSE and AMEX listings — in one keyless request, each row carrying a symbol, name, market
cap and sector. This adapter reads it, keeps the rows at/above the caller's market-cap
floor, and maps them onto ``ScreenedStock`` entities. It's the only module that knows
Nasdaq's screener exists; swap it for a yfinance ``yf.screen`` adapter and only this file
changes.

Why Nasdaq over yfinance for the *screen*: it's a single bulk call rather than a paginated
one, and it isn't the Yahoo endpoint the rest of the codebase already fights for
data-centre-IP blocking. The screen is the universe sync's primary input, so a failure
raises ``StockDataUnavailable`` (the sync then skips its reconcile rather than acting on a
partial result).

Confirmed against the live endpoint (``download=true`` returns the whole board — ~7,150 rows
in one call, ~1,450 at/above $5B):
  - Row fields are ``symbol`` / ``name`` / ``marketCap`` / ``sector`` (plus country,
    industry, etc. we don't use). ``marketCap`` is a plain numeric string ("36911030761.00").
  - There is **no per-row exchange**, so ``exchange`` is left ``None`` here and filled later,
    once, by whichever feature first learns it (the ticker card, from Alpaca).
  - ``name`` is the verbose legal title ("Agilent Technologies Inc. Common Stock"); we strip
    the standard equity-class suffix so the stored/searchable name is the clean company form
    ("Agilent Technologies Inc.") — matching the name the ticker card would otherwise fill.
  - The board also lists preferred-share and warrant tranches (Nasdaq reports the *issuer's*
    market cap on each, so one company would appear many times). Those aren't companies, so
    rows whose name marks them preferred/warrant are dropped.
"""

from __future__ import annotations

import httpx

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import ScreenedStock
from app.stocks.universe.ports import StockScreener

# The port's domain error is phrased per-symbol, but a screen isn't about one symbol; use a
# sentinel so a screen-wide failure reads sensibly ("'*' is unavailable: …").
_UNIVERSE = "*"

# Equity-class suffixes Nasdaq appends to the legal name. Stripped (longest first, so
# "Class A Common Stock" wins over "Common Stock") to recover the clean company name.
_NAME_SUFFIXES: tuple[str, ...] = tuple(
    sorted(
        {
            " Common Stock",
            " Common Shares",
            " Ordinary Shares",
            " Capital Stock",
            " Class A Common Stock",
            " Class B Common Stock",
            " Class C Common Stock",
            " Class A Common Shares",
            " Class B Common Shares",
            " Class C Capital Stock",
            " Class A Ordinary Shares",
            " Class B Ordinary Shares",
        },
        key=len,
        reverse=True,
    )
)

# A company's common-stock name never contains these; they mark a preferred-share or warrant
# listing (a separate security, not a company), which we exclude from the universe.
_NON_COMMON_MARKERS: tuple[str, ...] = ("preferred", "warrant")


class NasdaqScreenerProvider(StockScreener):
    """Screens the US market via Nasdaq's public screener API (no key)."""

    _DEFAULT_BASE_URL = "https://api.nasdaq.com"

    # Nasdaq 403s a non-browser client; a desktop User-Agent gets the JSON. The other
    # headers mirror what a browser sends so the request isn't singled out.
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self, base_url: str = _DEFAULT_BASE_URL, http_client=None) -> None:
        # An injectable client is the offline-test seam (mirrors the Finnhub adapters).
        self._http = http_client or httpx.Client(
            base_url=base_url, headers=self._HEADERS, timeout=30.0
        )

    def screen(self, *, min_market_cap: float) -> tuple[ScreenedStock, ...]:
        screened: list[ScreenedStock] = []
        for row in self._fetch_rows():
            if not isinstance(row, dict):
                continue
            raw_name = row.get("name")
            # Drop preferred-share / warrant tranches — not companies, and they'd duplicate
            # an issuer (Nasdaq stamps the issuer's cap on each).
            if _is_non_common(raw_name):
                continue
            market_cap = _parse_market_cap(row.get("marketCap"))
            # No market cap (blank — funds, freshly listed, odd instruments) or below the
            # floor: not part of this universe.
            if market_cap is None or market_cap < min_market_cap:
                continue
            ticker = _clean(row.get("symbol"))
            # Skip anything that won't fit / isn't a plain listing (a stray space, an
            # over-long or blank symbol) so a bad row never sinks the screen or overflows
            # the anchor's ticker column.
            if not ticker or " " in ticker or len(ticker) > 16:
                continue
            screened.append(
                ScreenedStock(
                    ticker=ticker.upper(),
                    name=_clean_name(raw_name),
                    exchange=None,  # not on the flat screen; filled lazily elsewhere
                    market_cap=market_cap,
                    sector=_clean(row.get("sector")),
                )
            )
        return tuple(screened)

    def _fetch_rows(self) -> list:
        """Fetch the whole board and return the raw ``data.rows`` list, translating any
        transport / HTTP / shape failure into ``StockDataUnavailable`` so a bad screen is a
        clean skip rather than a crash mid-sync."""
        try:
            resp = self._http.get(
                "/api/screener/stocks",
                params={"tableonly": "true", "download": "true"},
            )
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(_UNIVERSE, str(exc)) from exc
        if resp.status_code != 200:
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                _UNIVERSE, f"screener request failed (HTTP {resp.status_code}): {body}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(
                _UNIVERSE, f"invalid JSON payload: {exc}"
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("rows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise StockDataUnavailable(
                _UNIVERSE, "screener response missing data.rows"
            )
        return rows


def _parse_market_cap(value: object) -> float | None:
    """Parse Nasdaq's market-cap field to whole dollars, or ``None`` when blank/unparseable.

    The value arrives as a numeric string that may carry a ``$`` and thousands separators
    (defensively handled) or be blank / ``"NA"``. A parsed ``0`` is treated as "unknown"
    (the blank case some rows use), so it's excluded from the universe rather than sorted as
    a $0 company.
    """
    if isinstance(value, (int, float)):
        return float(value) or None
    if not isinstance(value, str):
        return None
    text = value.strip().replace("$", "").replace(",", "")
    if not text or text.upper() in {"NA", "N/A", "--"}:
        return None
    try:
        market_cap = float(text)
    except ValueError:
        return None
    return market_cap or None


def _clean(value: object) -> str | None:
    """Normalize a screener string to a non-empty, trimmed value or ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _clean_name(value: object) -> str | None:
    """The company display name: the trimmed screener name with its trailing equity-class
    suffix removed ("Apple Inc. Common Stock" -> "Apple Inc."). Names without a known suffix
    (or that would reduce to empty) are returned as-is; a non-string is ``None``."""
    name = _clean(value)
    if name is None:
        return None
    for suffix in _NAME_SUFFIXES:
        if name.endswith(suffix):
            trimmed = name[: -len(suffix)].strip()
            if trimmed:
                return trimmed
            break
    return name


def _is_non_common(value: object) -> bool:
    """True when the name marks a non-common security (a preferred-share or warrant
    listing) — a company's common-stock name never contains these markers."""
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(marker in lowered for marker in _NON_COMMON_MARKERS)
