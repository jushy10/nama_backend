"""Interface Adapter: a fund's rich profile from Yahoo (via ``yfinance``).

The ETF sync's per-fund enrichment source — the fund facts that live only on Yahoo's per-ticker
surfaces and that the bulk screen doesn't carry: category, fund family, NAV, the trailing return
ladder (YTD / 3y / 5y), the dividend yield, the prose description, the top holdings, and the
sector weightings. The sync persists all of it (scalars onto the ``etfs`` row, the two lists into
their child tables); the detail endpoint then serves it straight from the DB. It's the only module
that knows Yahoo/``yfinance`` backs the ETF profile; swap it for another ``EtfProfileProvider`` and
only this file changes. Sibling of ``yfinance_etf_screener_adapter`` (the bulk AUM screen).

Because ``category`` rides the same ``.info`` blob as the rest of the profile, it's read here too —
this adapter subsumes the old single-column category source, so the sync makes **one** per-ticker
call per fund rather than two.

Two Yahoo surfaces are read per fund:

- ``Ticker.info`` — the ``quoteSummary`` crumb-gated blob: ``category``, ``fundFamily``,
  ``totalAssets`` (AUM), ``netExpenseRatio``, ``navPrice``, ``yield``, ``ytdReturn``,
  ``threeYearAverageReturn``, ``fiveYearAverageReturn``. It's Yahoo's most crumb-gated endpoint, so
  the fetch goes through ``yfinance_session.call`` with an ``is_empty`` predicate: an empty
  ``.info`` is treated as a (likely swallowed) crumb 401, the cached crumb is dropped, and the call
  is retried once with a fresh handshake.
- ``Ticker.funds_data`` — the fund-specific surface: ``description`` (prose), ``top_holdings`` (a
  DataFrame indexed by holding symbol, with ``Name`` + ``Holding Percent`` columns) and
  ``sector_weightings`` (a ``{sector: weight}`` dict). Read best-effort and independently, so a
  fund whose ``.info`` serves but whose ``funds_data`` doesn't (or vice-versa) still yields
  whatever half came back.

**Unit normalization** (verified empirically against VOO — Yahoo mixes fractions and
already-percent numbers on the same blob, so each field is converted individually to human
percent):

- ``netExpenseRatio`` = ``0.03``  → already a percent → **as-is** (``0.03``). Matches the value
  the ``etfs`` table / screener already stores.
- ``yield`` = ``0.0103``  → a FRACTION → ``×100`` → ``1.03``.
- ``ytdReturn`` = ``11.25``  → already a PERCENT → **as-is** (NOT ``×100``).
- ``threeYearAverageReturn`` = ``0.204``  → a FRACTION → ``×100`` → ``20.4``.
- ``fiveYearAverageReturn`` = ``0.130``  → a FRACTION → ``×100`` → ``13.0``.
- ``top_holdings`` "Holding Percent" = ``0.0789``  → a FRACTION → ``×100`` → ``7.89``.
- ``sector_weightings`` value = ``0.3913``  → a FRACTION → ``×100`` → ``39.13``.
- ``totalAssets`` / ``navPrice`` are raw figures, passed through untouched.

**Failure contract — raises on a hard ``.info`` read, best-effort on the rest.** This is the
sync's primary per-fund source, and the sync must tell a blocked/failed fetch (skip the fund, leave
its stored profile intact, retry next run) from a fund Yahoo simply carries little data for (persist
what came back). So a hard ``.info`` failure — a raised error, or an empty ``.info`` still empty
after the crumb retry (Yahoo's swallowed-401 / IP-block signal) — raises ``StockDataUnavailable``.
Everything past a served ``.info`` is best-effort: ``funds_data`` and every individual field degrade
to ``None`` / empty rather than raising, so a reachable-but-sparse fund yields a partial profile,
not an error.
"""

from __future__ import annotations

import yfinance as yf

from app.stocks.adapters import yfinance_session
from app.stocks.etfs.entities import (
    EtfHolding,
    EtfProfile,
    EtfSectorWeight,
    slugify,
)
from app.stocks.etfs.ports import EtfProfileProvider
from app.stocks.exceptions import StockDataUnavailable

# The holdings surface can be long; a detail card shows the fund's largest positions, so cap it.
_MAX_HOLDINGS = 10


class YfinanceEtfProfileProvider(EtfProfileProvider):
    """Fetches a fund's rich profile from Yahoo's per-ticker ``.info`` + ``funds_data`` (no API
    key). Raises ``StockDataUnavailable`` on a hard ``.info`` read; best-effort past that."""

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to the real
        # yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_profile(self, symbol: str) -> EtfProfile:
        ticker = self._ticker_factory(symbol)
        info = self._read_info(symbol, ticker)  # raises on a hard/blocked read
        description, holdings, sectors = self._read_funds_data(ticker)  # best-effort, never raises
        return EtfProfile(
            category=slugify(info.get("category")),
            fund_family=_clean(info.get("fundFamily")),
            net_assets=_number(info.get("totalAssets")),
            # Already a percent on Yahoo's blob — kept as-is so it agrees with the etfs table.
            expense_ratio=_number(info.get("netExpenseRatio")),
            nav=_number(info.get("navPrice")),
            dividend_yield=_percent_from_fraction(info.get("yield")),
            # ytdReturn is already a percent number — do NOT scale it.
            ytd_return=_number(info.get("ytdReturn")),
            three_year_return=_percent_from_fraction(info.get("threeYearAverageReturn")),
            five_year_return=_percent_from_fraction(info.get("fiveYearAverageReturn")),
            description=description,
            top_holdings=holdings,
            sector_weightings=sectors,
        )

    def _read_info(self, symbol: str, ticker) -> dict:
        """Yahoo's ``.info`` blob, with the crumb-401 retry (an empty ``.info`` is a swallowed 401 →
        drop the cached crumb, re-fetch once). Raises ``StockDataUnavailable`` on a hard failure: a
        raised error, or an ``.info`` still empty after the retry (the block signal) — so the sync
        skips the fund and leaves its stored profile intact rather than marking it freshly-fetched
        with nothing to store."""
        try:
            info = yfinance_session.call(
                lambda: ticker.info,
                is_empty=lambda data: not data,
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance ETF profile failed ({exc})"
            ) from exc
        if not info:
            raise StockDataUnavailable(
                symbol, "yfinance ETF profile returned an empty .info (crumb 401 / IP block?)"
            )
        return info

    def _read_funds_data(
        self, ticker
    ) -> tuple[str | None, tuple[EtfHolding, ...], tuple[EtfSectorWeight, ...]]:
        """The ``funds_data`` surface (description + holdings + sector weightings), each read
        defensively so a missing or shape-shifted piece just yields its empty default. Best-effort
        by contract: ``funds_data`` itself can raise for a fund Yahoo carries no fund data for —
        that's caught here (not propagated), so a served ``.info`` still yields a partial profile
        rather than a failure."""
        try:
            funds = ticker.funds_data
            return (
                _clean(getattr(funds, "description", None)),
                _holdings(getattr(funds, "top_holdings", None)),
                _sector_weightings(getattr(funds, "sector_weightings", None)),
            )
        except Exception:  # noqa: BLE001 — best-effort: no funds_data → empty description/lists
            return (None, (), ())


def _holdings(frame) -> tuple[EtfHolding, ...]:
    """Map Yahoo's ``top_holdings`` DataFrame (indexed by holding symbol, columns ``Name`` +
    ``Holding Percent``) to the domain holdings, capped and weight-normalized to percent.

    Defensive: no frame, an empty one, or a row missing a field just contributes what it has (or
    is skipped). The vendor already orders it largest-first, so the order is preserved."""
    if frame is None or getattr(frame, "empty", True):
        return ()
    holdings: list[EtfHolding] = []
    try:
        for symbol, row in frame.head(_MAX_HOLDINGS).iterrows():
            holdings.append(
                EtfHolding(
                    ticker=_clean(symbol),
                    name=_clean(row.get("Name")),
                    weight=_percent_from_fraction(row.get("Holding Percent")),
                )
            )
    except Exception:  # noqa: BLE001 — a shape-shifted frame yields what we gathered, not a crash
        return tuple(holdings)
    return tuple(holdings)


def _sector_weightings(weightings) -> tuple[EtfSectorWeight, ...]:
    """Map Yahoo's ``{sector: fraction}`` dict to domain sector weights, normalized to percent and
    sorted by weight descending. A non-dict or empty value yields no weightings; a non-numeric
    entry is dropped."""
    if not isinstance(weightings, dict):
        return ()
    weights: list[EtfSectorWeight] = []
    for sector, value in weightings.items():
        weight = _percent_from_fraction(value)
        if isinstance(sector, str) and sector and weight is not None:
            weights.append(EtfSectorWeight(sector=sector, weight=weight))
    weights.sort(key=lambda w: w.weight, reverse=True)
    return tuple(weights)


def _percent_from_fraction(value: object) -> float | None:
    """A vendor FRACTION (e.g. ``0.0789``) → a human percent (``7.89``), or ``None`` when
    absent/non-numeric. ``bool`` is rejected — an ``int`` subclass that's never a valid figure."""
    number = _number(value)
    return None if number is None else number * 100


def _number(value: object) -> float | None:
    """A numeric vendor field → ``float``, or ``None`` when absent/non-numeric. ``bool`` is
    rejected (an ``int`` subclass, never a real figure), matching the screener adapter."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clean(value: object) -> str | None:
    """Trim a vendor string to a non-empty value, or ``None`` (non-strings included). Yahoo's
    DataFrame index values arrive as strings but pass through the same guard defensively."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
