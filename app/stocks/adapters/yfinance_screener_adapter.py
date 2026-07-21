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
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
