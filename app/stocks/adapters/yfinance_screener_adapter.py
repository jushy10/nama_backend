"""Interface Adapter: the US market screen from Yahoo, via yfinance.

yfinance's screener (``yf.screen`` + ``EquityQuery``) returns Yahoo's equity universe
filtered server-side. We ask for US-exchange-listed stock at/above the caller's market-cap
floor — NASDAQ, NYSE, NYSE American (AMEX) and Cboe BZX — and page through the
250-at-a-time results, mapping each quote onto a ``ScreenedStock``. It's the only module
that knows Yahoo/yfinance backs the screen; swap it for another ``StockScreener`` and only
this file changes.

Why yfinance for the *screen*: Yahoo's screen quote carries the **listing exchange** per
row (Nasdaq's bulk screener doesn't), so ``exchange`` is filled at screen time instead of
lazily later. The trade-off, accepted deliberately: the screen quote has **no sector**
(Yahoo only exposes it via per-ticker ``.info`` calls, impractical for ~1,400 names), so
``ScreenedStock.sector`` comes back ``None`` — the ``stocks.sector`` column waits for a
source that publishes it. Yahoo also intermittently blocks data-centre IPs; a blocked
screen surfaces as ``StockDataUnavailable`` (a hard failure the caller maps to 502), or —
if it simply returns nothing — an empty screen the sync skips.

Yahoo's exchange codes are mapped to the same vocabulary Alpaca fills the anchor with
(``NMS``/``NGM``/``NCM`` → ``NASDAQ``, ``NYQ`` → ``NYSE``, ``ASE`` → ``AMEX``, ``BTS`` →
``BATS``), so ``exchange`` reads the same whether the universe sync or the ticker card
settled it.
"""

from __future__ import annotations

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import ScreenedStock
from app.stocks.universe.ports import StockScreener

# The port's domain error is phrased per-symbol, but a screen isn't about one symbol; use a
# sentinel so a screen-wide failure reads sensibly ("'*' is unavailable: …").
_UNIVERSE = "*"

# Yahoo screen "exchange" codes for the US venues we treat as the market, mapped to the
# friendly names Alpaca fills the anchor with. The three Nasdaq tiers collapse to "NASDAQ".
_EXCHANGE_NAMES: dict[str, str] = {
    "NMS": "NASDAQ",  # Nasdaq Global Select
    "NGM": "NASDAQ",  # Nasdaq Global Market
    "NCM": "NASDAQ",  # Nasdaq Capital Market
    "NYQ": "NYSE",
    "ASE": "AMEX",  # NYSE American (formerly AMEX)
    "BTS": "BATS",  # Cboe BZX
}

_PAGE_SIZE = 250  # Yahoo caps a screen page at 250
_MAX_RESULTS = 10_000  # backstop so a bad ``total`` can't loop us forever


class YfinanceScreenerProvider(StockScreener):
    """Screens the US market via Yahoo's screener (``yf.screen``), keyless."""

    def __init__(self, *, screen_page=None) -> None:
        # The page fetch is the offline-test seam: inject a fn returning canned pages so a
        # test never calls Yahoo. Default hits the live screener.
        self._screen_page = screen_page or _live_screen_page

    def screen(self, *, min_market_cap: float) -> tuple[ScreenedStock, ...]:
        screened: list[ScreenedStock] = []
        offset = 0
        while offset < _MAX_RESULTS:
            page = self._fetch_page(min_market_cap=min_market_cap, offset=offset)
            quotes = page.get("quotes") or []
            if not quotes:
                break
            for quote in quotes:
                stock = _to_stock(quote, min_market_cap=min_market_cap)
                if stock is not None:
                    screened.append(stock)
            offset += len(quotes)
            if offset >= (page.get("total") or 0):
                break
        return tuple(screened)

    def _fetch_page(self, *, min_market_cap: float, offset: int) -> dict:
        """One screen page, translating any yfinance/transport failure into
        ``StockDataUnavailable`` so a blocked or broken screen is a clean domain error."""
        try:
            page = self._screen_page(
                min_market_cap=min_market_cap, offset=offset, size=_PAGE_SIZE
            )
        except Exception as exc:  # yfinance raises a grab-bag of types on a bad response
            raise StockDataUnavailable(_UNIVERSE, str(exc)) from exc
        if not isinstance(page, dict):
            raise StockDataUnavailable(
                _UNIVERSE, f"unexpected screen payload: {type(page).__name__}"
            )
        return page


def _live_screen_page(*, min_market_cap: float, offset: int, size: int) -> dict:
    """Call Yahoo's live screener for one page (imports yfinance lazily — the vendor stays
    inside the adapter). Filters ≥ ``min_market_cap`` on our US exchanges, largest cap first.
    """
    import yfinance as yf
    from yfinance import EquityQuery

    query = EquityQuery(
        "and",
        [
            EquityQuery("gte", ["intradaymarketcap", min_market_cap]),
            EquityQuery(
                "or",
                [EquityQuery("eq", ["exchange", code]) for code in _EXCHANGE_NAMES],
            ),
        ],
    )
    return yf.screen(
        query, offset=offset, size=size, sortField="intradaymarketcap", sortAsc=False
    )


def _to_stock(quote: object, *, min_market_cap: float) -> ScreenedStock | None:
    """Map one Yahoo screen quote to a ``ScreenedStock``, or ``None`` to drop it.

    Dropped: a non-dict row, a blank/oversized/spacey symbol, or a missing / below-floor
    market cap (the server filters the floor, but we re-check defensively). ``sector`` is
    always ``None`` — Yahoo's screen quote doesn't carry it.
    """
    if not isinstance(quote, dict):
        return None
    ticker = _clean(quote.get("symbol"))
    if not ticker or " " in ticker or len(ticker) > 16:
        return None
    market_cap = quote.get("marketCap")
    if not isinstance(market_cap, (int, float)) or market_cap < min_market_cap:
        return None
    return ScreenedStock(
        ticker=ticker.upper(),
        name=_clean(quote.get("longName")) or _clean(quote.get("shortName")),
        exchange=_EXCHANGE_NAMES.get(quote.get("exchange")),
        market_cap=float(market_cap),
        sector=None,  # Yahoo's screen quote has no sector
    )


def _clean(value: object) -> str | None:
    """Trim a screen string to a non-empty value, or ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
