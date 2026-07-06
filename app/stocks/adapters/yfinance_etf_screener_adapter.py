"""Interface Adapter: the top US ETFs screen from Yahoo, via yfinance.

yfinance ships Yahoo's *predefined* screens; ``top_etfs_us`` is the curated US ETF set
(4-5-star rated, price > $10, ~540 funds). We page through it 250 at a time and map each quote
onto a ``ScreenedEtf``. It's the only module that knows Yahoo/yfinance backs the ETF screen;
swap it for another ``EtfScreener`` and only this file changes.

Why the *predefined* screen rather than a custom ``FundQuery``: yfinance's fund-query builder
exposes no net-assets field, so it can't rank by AUM the way ``EquityQuery`` ranks stocks by
market cap. The predefined ``top_etfs_us`` screen carries ``netAssets`` on every row, so we pull
the curated set and let the read side sort by AUM (or expense / return). Yahoo sorts the
predefined set by the day's move; we don't rely on that order.

Yahoo intermittently blocks data-centre IPs; a blocked screen surfaces as
``StockDataUnavailable`` (the sync treats it as a lost round), or — if it simply returns nothing
— an empty screen the sync skips.

Yahoo's exchange codes are mapped to the same vocabulary the stock screen/anchor use
(``NMS``/``NGM``/``NCM`` → ``NASDAQ``, ``NYQ`` → ``NYSE``, ``ASE`` → ``AMEX``, ``BTS`` →
``BATS``), plus ``PCX`` → ``NYSEARCA`` (NYSE Arca, the primary ETF venue, which the stock
screen never sees).
"""

from __future__ import annotations

from app.stocks.etfs.entities import ScreenedEtf
from app.stocks.etfs.ports import EtfScreener
from app.stocks.exceptions import StockDataUnavailable

# The port's domain error is phrased per-symbol, but a screen isn't about one symbol; use a
# sentinel so a screen-wide failure reads sensibly ("'*' is unavailable: …").
_UNIVERSE = "*"

# The Yahoo predefined screen that returns the curated top US ETF set.
_SCREEN = "top_etfs_us"

# Yahoo "exchange" codes for the US venues ETFs list on, mapped to the friendly names the stock
# anchor uses. ETFs list mostly on NYSE Arca (PCX) and Nasdaq / Cboe.
_EXCHANGE_NAMES: dict[str, str] = {
    "NMS": "NASDAQ",  # Nasdaq Global Select
    "NGM": "NASDAQ",  # Nasdaq Global Market
    "NCM": "NASDAQ",  # Nasdaq Capital Market
    "NYQ": "NYSE",
    "ASE": "AMEX",  # NYSE American (formerly AMEX)
    "PCX": "NYSEARCA",  # NYSE Arca — the primary ETF venue
    "BTS": "BATS",  # Cboe BZX
}

_PAGE_SIZE = 250  # Yahoo caps a screen page at 250
_MAX_RESULTS = 5_000  # backstop so a bad ``total`` can't loop us forever


class YfinanceEtfScreenerProvider(EtfScreener):
    """Screens the top US ETFs via Yahoo's predefined ``top_etfs_us`` screen, keyless."""

    def __init__(self, *, screen_page=None) -> None:
        # The page fetch is the offline-test seam: inject a fn returning canned pages so a test
        # never calls Yahoo. Default hits the live screener.
        self._screen_page = screen_page or _live_screen_page

    def screen(self) -> tuple[ScreenedEtf, ...]:
        screened: list[ScreenedEtf] = []
        seen: set[str] = set()
        offset = 0
        while offset < _MAX_RESULTS:
            page = self._fetch_page(offset=offset)
            quotes = page.get("quotes") or []
            if not quotes:
                break
            for quote in quotes:
                etf = _to_etf(quote)
                # Dedupe by ticker: Yahoo's predefined paging can overlap at the seams, and a
                # duplicate ticker would trip the ``etfs`` unique constraint on upsert.
                if etf is not None and etf.ticker not in seen:
                    seen.add(etf.ticker)
                    screened.append(etf)
            offset += len(quotes)
            if offset >= (page.get("total") or 0):
                break
        return tuple(screened)

    def _fetch_page(self, *, offset: int) -> dict:
        """One screen page, translating any yfinance/transport failure into
        ``StockDataUnavailable`` so a blocked or broken screen is a clean domain error."""
        try:
            page = self._screen_page(offset=offset, size=_PAGE_SIZE)
        except Exception as exc:  # yfinance raises a grab-bag of types on a bad response
            raise StockDataUnavailable(_UNIVERSE, str(exc)) from exc
        if not isinstance(page, dict):
            raise StockDataUnavailable(
                _UNIVERSE, f"unexpected screen payload: {type(page).__name__}"
            )
        return page


def _live_screen_page(*, offset: int, size: int) -> dict:
    """Call Yahoo's live predefined ETF screen for one page (imports yfinance lazily — the
    vendor stays inside the adapter). Predefined screens take ``count`` (the ``size`` arg is
    deprecated for them)."""
    import yfinance as yf

    return yf.screen(_SCREEN, offset=offset, count=size)


def _to_etf(quote: object) -> ScreenedEtf | None:
    """Map one Yahoo screen quote to a ``ScreenedEtf``, or ``None`` to drop it.

    Dropped: a non-dict row or a blank/oversized/spacey symbol. Every figure is best-effort — a
    missing or non-numeric value rides in ``None`` rather than dropping the fund. The fund's
    ``category`` isn't read here — the bulk screen doesn't carry it; the enrichment pass fills it
    per-ticker.
    """
    if not isinstance(quote, dict):
        return None
    ticker = _clean(quote.get("symbol"))
    if not ticker or " " in ticker or len(ticker) > 16:
        return None
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
