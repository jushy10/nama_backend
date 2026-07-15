"""Interface Adapter: the US + Canadian market screen from Yahoo, via yfinance.

yfinance's screener (``yf.screen`` + ``EquityQuery``) returns Yahoo's equity universe
filtered server-side. We ask for stock at/above the caller's market-cap floor in one market
at a time (``region``): the US pass scopes to our explicit venues — NASDAQ, NYSE, NYSE
American (AMEX) and Cboe BZX — while the Canadian pass scopes by ``region == ca`` (the
TSX/TSXV listings). We page through the 250-at-a-time results, mapping each quote onto a
``ScreenedStock`` stamped with its ``country`` / ``currency``. It's the only module that
knows Yahoo/yfinance backs the screen; swap it for another ``StockScreener`` and only this
file changes.

**The market-cap floor is native, not converted.** Yahoo screens each quote in its own
trading currency, so ``min_market_cap=1e9`` is $1B USD on the US pass and $1B CAD on the CA
pass — exactly the "≥$1B in each market's own money" the universe wants. The stamped
``currency`` is what carries that unit downstream (a mixed-currency ``market_cap`` sort is
therefore nominal; the read side filters by ``country`` to stay within one currency).

Why yfinance for the *screen*: Yahoo's screen quote carries the **listing exchange** per
row (Nasdaq's bulk screener doesn't), so ``exchange`` is filled at screen time instead of
lazily later. The trade-off, accepted deliberately: the screen quote has **no sector**
(Yahoo only exposes it via per-ticker ``.info`` calls, impractical for ~1,400 names), so
``ScreenedStock.sector`` comes back ``None`` — the ``stocks.sector`` column waits for a
source that publishes it. Yahoo also intermittently blocks data-centre IPs; a blocked
screen surfaces as ``StockDataUnavailable`` (a hard failure the caller maps to 502), or —
if it simply returns nothing — an empty screen the sync skips. A screen page is a crumb-gated
Yahoo call like the per-ticker reads, so each page goes through ``yfinance_session.call``: a
transient crumb 401 (raised, or swallowed into a payload with no ``total``) drops the cached
crumb and re-fetches once before it's treated as a failure — so one bad handshake on page 0
no longer sinks the whole sweep. A legitimate past-the-end page still carries ``total`` (only
``quotes`` is empty), so it's never mistaken for a block.

Yahoo's exchange codes are mapped to the same vocabulary Alpaca fills the anchor with
(``NMS``/``NGM``/``NCM`` → ``NASDAQ``, ``NYQ`` → ``NYSE``, ``ASE`` → ``AMEX``, ``BTS`` →
``BATS``; ``TOR`` → ``TSX``, ``VAN`` → ``TSXV``, ``NEO`` → ``NEO``, ``CNQ`` → ``CSE``), so
``exchange`` reads the same whether the universe sync or the ticker card settled it. The CA
codes are for the display map only — the CA pass filters by ``region``, not by these codes,
so an unlisted TSX venue simply maps to a ``None`` exchange rather than dropping the row.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.adapters import yfinance_session
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import ScreenedStock
from app.stocks.universe.ports import StockScreener

# The port's domain error is phrased per-symbol, but a screen isn't about one symbol; use a
# sentinel so a screen-wide failure reads sensibly ("'*' is unavailable: …").
_UNIVERSE = "*"

# Yahoo screen "exchange" codes for the US venues we treat as the market, mapped to the
# friendly names Alpaca fills the anchor with. The three Nasdaq tiers collapse to "NASDAQ".
# The US screen filters on *these codes* (an OR over the keys), so keep it US-only — a CA
# venue here would leak TSX names into the US pass.
_US_EXCHANGE_NAMES: dict[str, str] = {
    "NMS": "NASDAQ",  # Nasdaq Global Select
    "NGM": "NASDAQ",  # Nasdaq Global Market
    "NCM": "NASDAQ",  # Nasdaq Capital Market
    "NYQ": "NYSE",
    "ASE": "AMEX",  # NYSE American (formerly AMEX)
    "BTS": "BATS",  # Cboe BZX
}

# Yahoo screen "exchange" codes for the Canadian venues. The CA screen filters by ``region``
# (not by these codes), so this map is only for turning a returned code into a friendly
# ``exchange`` name — Yahoo occasionally returns codes not listed here, which map to ``None``.
_CA_EXCHANGE_NAMES: dict[str, str] = {
    "TOR": "TSX",  # Toronto Stock Exchange
    "VAN": "TSXV",  # TSX Venture Exchange
    "NEO": "NEO",  # Cboe Canada (NEO)
    "CNQ": "CSE",  # Canadian Securities Exchange
}

# Merged view for the display lookup in ``_to_stock`` (a returned code -> friendly name),
# regardless of which market's pass produced the row.
_EXCHANGE_NAMES: dict[str, str] = {**_US_EXCHANGE_NAMES, **_CA_EXCHANGE_NAMES}


@dataclass(frozen=True)
class _Region:
    """One market's screen config: the ISO-2 country + default trading currency stamped onto
    every ``ScreenedStock`` it yields, and (US only) the exchange codes the query filters on.

    The US pass filters by explicit exchange codes (Yahoo's ``region == us`` also sweeps in OTC
    / pink-sheet names we don't want); the CA pass filters by ``region == ca`` (the TSX/TSXV
    exchange codes are less stable, and the region filter already scopes to Canadian listings)."""

    country: str
    currency: str
    exchange_codes: tuple[str, ...] | None  # None -> filter by region instead of exchange


_REGIONS: dict[str, _Region] = {
    "us": _Region("US", "USD", tuple(_US_EXCHANGE_NAMES)),
    "ca": _Region("CA", "CAD", None),
}

_PAGE_SIZE = 250  # Yahoo caps a screen page at 250
_MAX_RESULTS = 10_000  # backstop so a bad ``total`` can't loop us forever


class YfinanceScreenerProvider(StockScreener):
    """Screens a market (US or Canada) via Yahoo's screener (``yf.screen``), keyless."""

    def __init__(self, *, screen_page=None) -> None:
        # The page fetch is the offline-test seam: inject a fn returning canned pages so a
        # test never calls Yahoo. Default hits the live screener.
        self._screen_page = screen_page or _live_screen_page

    def screen(
        self, *, min_market_cap: float, region: str = "us"
    ) -> tuple[ScreenedStock, ...]:
        market = _REGIONS.get(region.lower())
        if market is None:
            raise StockDataUnavailable(_UNIVERSE, f"unknown screen region: {region!r}")
        screened: list[ScreenedStock] = []
        offset = 0
        while offset < _MAX_RESULTS:
            page = self._fetch_page(
                min_market_cap=min_market_cap, offset=offset, region=region.lower()
            )
            quotes = page.get("quotes") or []
            if not quotes:
                break
            for quote in quotes:
                stock = _to_stock(quote, min_market_cap=min_market_cap, market=market)
                if stock is not None:
                    screened.append(stock)
            offset += len(quotes)
            if offset >= (page.get("total") or 0):
                break
        return tuple(screened)

    def _fetch_page(self, *, min_market_cap: float, offset: int, region: str) -> dict:
        """One screen page, translating any yfinance/transport failure into
        ``StockDataUnavailable`` so a blocked or broken screen is a clean domain error."""
        try:
            page = self._screen_page(
                min_market_cap=min_market_cap,
                offset=offset,
                size=_PAGE_SIZE,
                region=region,
            )
        except Exception as exc:  # yfinance raises a grab-bag of types on a bad response
            raise StockDataUnavailable(_UNIVERSE, str(exc)) from exc
        if not isinstance(page, dict):
            raise StockDataUnavailable(
                _UNIVERSE, f"unexpected screen payload: {type(page).__name__}"
            )
        return page


def _live_screen_page(
    *, min_market_cap: float, offset: int, size: int, region: str = "us"
) -> dict:
    """Call Yahoo's live screener for one page (imports yfinance lazily — the vendor stays
    inside the adapter). Filters ≥ ``min_market_cap`` in the market's native currency, largest
    cap first. The US pass scopes by explicit exchange codes; the CA pass by ``region == ca``.
    """
    import yfinance as yf
    from yfinance import EquityQuery

    market = _REGIONS[region]  # validated by the caller (screen)
    if market.exchange_codes is not None:
        # US: scope to our explicit venues (Yahoo's region==us also sweeps in OTC names).
        scope = EquityQuery(
            "or",
            [EquityQuery("eq", ["exchange", code]) for code in market.exchange_codes],
        )
    else:
        # CA: scope by region — the TSX/TSXV listings — without pinning exchange codes.
        scope = EquityQuery("eq", ["region", region])
    query = EquityQuery(
        "and",
        [EquityQuery("gte", ["intradaymarketcap", min_market_cap]), scope],
    )
    # Through the shared crumb-401 retry, like the per-ticker reads. A well-formed page always
    # carries ``total`` (even past the end, where only ``quotes`` is empty), so "no ``total``" is
    # the swallowed-401 / malformed signature worth a fresh-crumb retry — and the legitimate empty
    # tail page, which does carry ``total``, is not retried.
    return yfinance_session.call(
        lambda: yf.screen(
            query, offset=offset, size=size, sortField="intradaymarketcap", sortAsc=False
        ),
        is_empty=lambda page: not (isinstance(page, dict) and "total" in page),
    )


def _to_stock(
    quote: object, *, min_market_cap: float, market: _Region
) -> ScreenedStock | None:
    """Map one Yahoo screen quote to a ``ScreenedStock``, or ``None`` to drop it.

    Dropped: a non-dict row, a blank/oversized/spacey symbol, or a missing / below-floor
    market cap (the server filters the floor, but we re-check defensively). ``sector`` is
    always ``None`` — Yahoo's screen quote doesn't carry it. ``price`` is the quote's
    ``regularMarketPrice`` when present and positive (the sync derives the stored P/E from
    it), else ``None``. ``country`` is the pass's market; ``currency`` is the quote's own
    ``currency`` when present (a rare USD-quoted TSX name keeps its unit), else the market's
    default — so the stored ``market_cap`` always carries the currency its floor was applied in.
    """
    if not isinstance(quote, dict):
        return None
    ticker = _clean(quote.get("symbol"))
    if not ticker or " " in ticker or len(ticker) > 16:
        return None
    market_cap = quote.get("marketCap")
    if not isinstance(market_cap, (int, float)) or market_cap < min_market_cap:
        return None
    # The screen quote also carries the regular-market price; keep it (positive numbers only)
    # so the sync can derive pe_ratio without a second vendor call — a missing / non-positive
    # price just rides as None and leaves that stock's P/E unset.
    raw_price = quote.get("regularMarketPrice")
    price = (
        float(raw_price)
        if isinstance(raw_price, (int, float)) and raw_price > 0
        else None
    )
    quote_currency = _clean(quote.get("currency"))
    return ScreenedStock(
        ticker=ticker.upper(),
        name=_clean(quote.get("longName")) or _clean(quote.get("shortName")),
        exchange=_EXCHANGE_NAMES.get(quote.get("exchange")),
        market_cap=float(market_cap),
        sector=None,  # Yahoo's screen quote has no sector
        price=price,
        country=market.country,
        currency=quote_currency.upper() if quote_currency else market.currency,
    )


def _clean(value: object) -> str | None:
    """Trim a screen string to a non-empty value, or ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
