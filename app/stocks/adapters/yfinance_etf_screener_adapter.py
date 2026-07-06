"""Interface Adapter: the top US ETFs screen from Yahoo, via yfinance.

We screen the US ETF universe with a custom ``ETFQuery`` — ``region == us`` and
``fundnetassets >= min_net_assets`` (an AUM floor the caller sets, the ETF analogue of the stock
screener's market-cap floor) — ranked by AUM (``fundnetassets``, largest first) and paged 250 at
a time. Each quote maps onto a ``ScreenedEtf``. It's the only module that knows Yahoo/yfinance
backs the ETF screen; swap it for another ``EtfScreener`` and only this file changes.

Why a custom ``ETFQuery`` rather than the predefined ``top_etfs_us`` screen: the predefined
screen is a fixed, curated ~540-fund list (4-5-star, price > $10) that can't be widened.
yfinance's ETF query exposes a ``fundnetassets`` field, so we can filter *and* rank the whole US
ETF universe by AUM ourselves — the way ``EquityQuery`` ranks stocks by ``intradaymarketcap`` —
giving a floor-defined set of any size instead of Yahoo's fixed top list. (An earlier note here
claimed the fund query had no net-assets field; that was true of the *mutual-fund* ``FundQuery``,
but the ETF-specific query does carry it.) Every row still carries ``netAssets`` +
``netExpenseRatio``, so the read side sorts by AUM (or expense / return).

Yahoo intermittently blocks data-centre IPs; a blocked screen surfaces as
``StockDataUnavailable`` (the sync treats it as a lost round), or — if it simply returns nothing
— an empty screen the sync skips.

Yahoo's exchange codes are mapped to the same vocabulary the stock screen/anchor use
(``NMS``/``NGM``/``NCM`` → ``NASDAQ``, ``NYQ`` → ``NYSE``, ``ASE`` → ``AMEX``, ``BTS`` →
``BATS``). ETFs mostly list on NYSE Arca, which Yahoo reports as ``PCX``; we fold that into
``NYSE`` (Arca is a wholly-owned NYSE venue, the same way the three Nasdaq tiers fold into
``NASDAQ``) so ``exchange`` stays inside the same four-value vocabulary the stock screen uses.

The broad ``region == us`` ETF screen can occasionally return a stray non-fund row (a
``quoteType`` other than ``ETF``); those are dropped so the ``etfs`` table holds only funds.
"""

from __future__ import annotations

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
    """Screens the US ETF universe at/above an AUM floor via Yahoo's ETF screener, keyless."""

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
        """One screen page, translating any yfinance/transport failure into
        ``StockDataUnavailable`` so a blocked or broken screen is a clean domain error."""
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
    """Call Yahoo's live ETF screener for one page (imports yfinance lazily — the vendor stays
    inside the adapter). Filters US ETFs with AUM ≥ ``min_net_assets``, largest AUM first. A
    custom query takes ``size`` (``count`` is only for the predefined screens)."""
    import yfinance as yf
    from yfinance.screener.query import ETFQuery

    query = ETFQuery(
        "and",
        [
            ETFQuery("eq", ["region", _US_REGION]),
            ETFQuery("gte", [_AUM_FIELD, min_net_assets]),
        ],
    )
    return yf.screen(
        query, offset=offset, size=size, sortField=_AUM_FIELD, sortAsc=False
    )


def _to_etf(quote: object) -> ScreenedEtf | None:
    """Map one Yahoo screen quote to a ``ScreenedEtf``, or ``None`` to drop it.

    Dropped: a non-dict row, a blank/oversized/spacey symbol, or a non-fund row (a ``quoteType``
    the broad ``region == us`` screen occasionally returns that isn't ``ETF`` — kept only when the
    field is absent, so a fund without the tag still rides through). Every figure is best-effort —
    a missing or non-numeric value rides in ``None`` rather than dropping the fund. The fund's
    ``category`` isn't read here — the bulk screen doesn't carry it; the enrichment pass fills it
    per-ticker.
    """
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
    """A numeric screen field → ``float``, or ``None`` when absent/non-numeric. ``bool`` is
    rejected — it's an ``int`` subclass but never a valid figure here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clean(value: object) -> str | None:
    """Trim a screen string to a non-empty value, or ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
