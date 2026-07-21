from __future__ import annotations

from app.stocks.adapters import yfinance_session
from app.stocks.etfs.entities import ScreenedEtf
from app.stocks.etfs.ports import EtfScreener
from app.stocks.exceptions import StockDataUnavailable

# The port's domain error is phrased per-symbol, but a screen isn't about one symbol; use a
# sentinel so a screen-wide failure reads sensibly ("'*' is unavailable: …").
_UNIVERSE = "*"

# The Yahoo ETF-screen field we filter and rank on (net assets = AUM) and the region we scope to.
_AUM_FIELD = "fundnetassets"
_US_REGION = "us"

# Yahoo "exchange" codes for the US venues ETFs list on, mapped to the friendly names the stock
# anchor uses. ETFs list mostly on NYSE Arca (PCX) and Nasdaq / Cboe.
_EXCHANGE_NAMES: dict[str, str] = {
    "NMS": "NASDAQ",  # Nasdaq Global Select
    "NGM": "NASDAQ",  # Nasdaq Global Market
    "NCM": "NASDAQ",  # Nasdaq Capital Market
    "NYQ": "NYSE",
    "ASE": "AMEX",  # NYSE American (formerly AMEX)
    "PCX": "NYSE",  # NYSE Arca — the primary ETF venue; folded into its parent NYSE
    "BTS": "BATS",  # Cboe BZX
}

_PAGE_SIZE = 250  # Yahoo caps a screen page at 250
_MAX_RESULTS = 5_000  # backstop so a bad ``total`` can't loop us forever


class YfinanceEtfScreenerProvider(EtfScreener):
    def __init__(self, *, screen_page=None) -> None:
        # The page fetch is the offline-test seam: inject a fn returning canned pages so a test
        # never calls Yahoo. Default hits the live screener.
        self._screen_page = screen_page or _live_screen_page

    def screen(self, *, min_net_assets: float) -> tuple[ScreenedEtf, ...]:
        screened: list[ScreenedEtf] = []
        seen: set[str] = set()
        offset = 0
        while offset < _MAX_RESULTS:
            page = self._fetch_page(min_net_assets=min_net_assets, offset=offset)
            quotes = page.get("quotes") or []
            if not quotes:
                break
            for quote in quotes:
                etf = _to_etf(quote)
                # Dedupe by ticker: Yahoo's paging can overlap at the seams, and a duplicate
                # ticker would trip the ``etfs`` unique constraint on upsert.
                if etf is not None and etf.ticker not in seen:
                    seen.add(etf.ticker)
                    screened.append(etf)
            offset += len(quotes)
            if offset >= (page.get("total") or 0):
                break
        return tuple(screened)

    def _fetch_page(self, *, min_net_assets: float, offset: int) -> dict:
        try:
            page = self._screen_page(
                min_net_assets=min_net_assets, offset=offset, size=_PAGE_SIZE
            )
        except Exception as exc:  # yfinance raises a grab-bag of types on a bad response
            raise StockDataUnavailable(_UNIVERSE, str(exc)) from exc
        if not isinstance(page, dict):
            raise StockDataUnavailable(
                _UNIVERSE, f"unexpected screen payload: {type(page).__name__}"
            )
        return page


def _live_screen_page(*, min_net_assets: float, offset: int, size: int) -> dict:
    import yfinance as yf
    from yfinance.screener.query import ETFQuery

    query = ETFQuery(
        "and",
        [
            ETFQuery("eq", ["region", _US_REGION]),
            ETFQuery("gte", [_AUM_FIELD, min_net_assets]),
        ],
    )
    # Through the shared crumb-401 retry, like the per-ticker reads. A well-formed page always
    # carries ``total`` (even past the end, where only ``quotes`` is empty), so "no ``total``" is
    # the swallowed-401 / malformed signature worth a fresh-crumb retry — and the legitimate empty
    # tail page, which does carry ``total``, is not retried.
    return yfinance_session.call(
        lambda: yf.screen(
            query, offset=offset, size=size, sortField=_AUM_FIELD, sortAsc=False
        ),
        is_empty=lambda page: not (isinstance(page, dict) and "total" in page),
    )


def _to_etf(quote: object) -> ScreenedEtf | None:
    if not isinstance(quote, dict):
        return None
    ticker = _clean(quote.get("symbol"))
    if not ticker or " " in ticker or len(ticker) > 16:
        return None
    quote_type = quote.get("quoteType")
    if quote_type is not None and quote_type != "ETF":
        return None  # a stray non-fund row in the broad US screen
    return ScreenedEtf(
        ticker=ticker.upper(),
        name=_clean(quote.get("longName")) or _clean(quote.get("shortName")),
        exchange=_EXCHANGE_NAMES.get(quote.get("exchange")),
        net_assets=_number(quote.get("netAssets")),
        expense_ratio=_number(quote.get("netExpenseRatio")),
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
