from __future__ import annotations

import yfinance as yf

from app.adapters.yfinance import session
from app.domains.etfs.entities import (
    EtfHolding,
    EtfProfile,
    EtfSectorWeight,
    slugify,
)
from app.domains.etfs.interfaces import EtfProfileAdapter
from app.domains.shared.exceptions import StockDataUnavailable

# The holdings surface can be long; a detail card shows the fund's largest positions, so cap it.
_MAX_HOLDINGS = 10


class EtfProfileAdapterImpl(EtfProfileAdapter):
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
        try:
            info = session.call(
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
        try:
            return session.call(
                lambda: self._funds_snapshot(ticker),
                # Holdings and sector weightings are parsed from one ``topHoldings`` response, so
                # they land (or fail) together: both empty is the swallowed-401 signature → retry
                # with a fresh crumb. A fund Yahoo genuinely has no fund data for just retries once
                # and stays empty. (Description alone isn't enough signal, so it's excluded.)
                is_empty=lambda snap: not snap[1] and not snap[2],
            )
        except Exception:  # noqa: BLE001 — best-effort: a hard/failed funds_data read → empty half
            return (None, (), ())

    def _funds_snapshot(
        self, ticker
    ) -> tuple[str | None, tuple[EtfHolding, ...], tuple[EtfSectorWeight, ...]]:
        funds = ticker.funds_data
        return (
            _clean(getattr(funds, "description", None)),
            _holdings(getattr(funds, "top_holdings", None)),
            _sector_weightings(getattr(funds, "sector_weightings", None)),
        )


def _holdings(frame) -> tuple[EtfHolding, ...]:
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
    number = _number(value)
    return None if number is None else number * 100


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
