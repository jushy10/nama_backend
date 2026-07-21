from __future__ import annotations

import math
from datetime import date, datetime

import yfinance as yf

from app.stocks.adapters import yfinance_session
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.institutional_ownership.entities import (
    HOLDER_TYPE_INSTITUTION,
    HOLDER_TYPE_MUTUAL_FUND,
    InstitutionalHolder,
    InstitutionalOwnership,
    OwnershipBreakdown,
)
from app.stocks.institutional_ownership.ports import InstitutionalOwnershipProvider


class YfinanceInstitutionalHoldersProvider(InstitutionalOwnershipProvider):
    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to the real
        # yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_institutional_ownership(self, symbol: str) -> InstitutionalOwnership:
        # Primary feed: a hard failure (including a Ticker-construction failure) raises; an
        # empty-after-retry frame is genuine no-coverage.
        ticker, institutions = self._read_primary(symbol)
        holders = _parse_holders(institutions, HOLDER_TYPE_INSTITUTION)

        # Best-effort fund feed — its failure/absence must never sink the primary feed.
        holders += _parse_holders(
            self._read_best_effort(lambda: ticker.mutualfund_holders),
            HOLDER_TYPE_MUTUAL_FUND,
        )

        breakdown = _parse_breakdown(
            self._read_best_effort(lambda: ticker.major_holders)
        )

        holders.sort(key=_holder_sort_key, reverse=True)  # newest quarter, largest position first
        return InstitutionalOwnership(
            symbol=symbol, breakdown=breakdown, holders=tuple(holders)
        )

    def _read_primary(self, symbol: str):
        try:
            ticker = self._ticker_factory(symbol)
            frame = yfinance_session.call(
                lambda: ticker.institutional_holders,
                is_empty=yfinance_session.frame_is_empty,
            )
            return ticker, frame
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance institutional holders failed ({exc})"
            ) from exc

    @staticmethod
    def _read_best_effort(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001 — best-effort enrichment: absence is fine
            return None


def _parse_holders(frame, holder_type: str) -> list[InstitutionalHolder]:
    if yfinance_session.frame_is_empty(frame):
        return []
    try:
        records = frame.to_dict("records")
    except Exception:  # noqa: BLE001 — an unexpected frame shape yields no rows, not a crash
        return []
    holders: list[InstitutionalHolder] = []
    for rec in records:
        holder = _clean(rec.get("Holder"))
        reported = _date(rec.get("Date Reported"))
        if holder is None or reported is None:
            continue
        holders.append(
            InstitutionalHolder(
                holder=holder,
                holder_type=holder_type,
                date_reported=reported,
                shares=_num(rec.get("Shares")),
                value=_num(rec.get("Value")),
                pct_held=_pct(_first(rec, "pctHeld", "% Out")),
                pct_change=_pct(_first(rec, "pctChange", "% Change")),
            )
        )
    return holders


def _parse_breakdown(frame) -> OwnershipBreakdown | None:
    if yfinance_session.frame_is_empty(frame):
        return None
    try:
        data = frame.to_dict()
    except Exception:  # noqa: BLE001 — an unexpected shape is a None breakdown, not a crash
        return None
    value_map = data.get("Value") if isinstance(data, dict) else None
    if not isinstance(value_map, dict):
        return None
    breakdown = OwnershipBreakdown(
        institutions_pct_held=_pct(value_map.get("institutionsPercentHeld")),
        insiders_pct_held=_pct(value_map.get("insidersPercentHeld")),
        institutions_float_pct_held=_pct(value_map.get("institutionsFloatPercentHeld")),
        institutions_count=_int(value_map.get("institutionsCount")),
    )
    return None if breakdown.is_empty else breakdown


def _holder_sort_key(holder: InstitutionalHolder) -> tuple:
    return (holder.date_reported, holder.value if holder.value is not None else -1.0)


def _first(rec: dict, *keys: str):
    for key in keys:
        if key in rec and rec[key] is not None:
            return rec[key]
    return None


def _date(value) -> date | None:
    if value is None:
        return None
    # A pandas NaT (an empty date cell) is *not equal to itself*, like NaN — catch it before the
    # datetime branch, since NaT is a datetime subclass whose ``.date()`` would return NaT again and
    # poison the ordering.
    try:
        if value != value:
            return None
    except Exception:  # noqa: BLE001 — an un-comparable value just isn't a NaT
        pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            return to_pydatetime().date()
        except Exception:  # noqa: BLE001 — an odd timestamp is unparseable, not fatal
            return None
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip()[:10]).date()
        except ValueError:
            return None
    return None


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct(value) -> float | None:
    number = _num(value)
    return None if number is None else number * 100.0


def _int(value) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _clean(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
