from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import yfinance as yf

from app.stocks.adapters import yfinance_session
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.recommendations.entities import AnalystRatingChanges, RatingChange
from app.stocks.recommendations.ports import RatingChangeProvider


class YfinanceRatingChangeProvider(RatingChangeProvider):
    # Cap on stored history per stock: Yahoo serves the full multi-year log, but the feature
    # only needs the recent window, and this bounds the table (~this many rows/stock max).
    _MAX_CHANGES = 50

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        try:
            # An empty frame is how yfinance surfaces a swallowed crumb 401, so retry once
            # with a fresh crumb; genuine no-coverage just comes back empty after that.
            frame = yfinance_session.call(
                lambda: self._ticker_factory(symbol).upgrades_downgrades,
                is_empty=yfinance_session.frame_is_empty,
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance upgrades/downgrades failed ({exc})"
            ) from exc
        changes = _parse_changes(frame)[: self._MAX_CHANGES]
        return AnalystRatingChanges(symbol=symbol, changes=tuple(changes))


def _parse_changes(frame) -> list[RatingChange]:
    if frame is None or getattr(frame, "empty", True):
        return []
    try:
        pairs = list(frame.iterrows())
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return []
    seen: set[tuple[str, date]] = set()
    changes: list[RatingChange] = []
    for index, series in pairs:
        firm = _text(_series_get(series, "Firm"))
        if not firm:
            continue
        # Current yfinance carries the date in the index; some payloads add a GradeDate column.
        published_at = _to_date(index) or _to_date(_series_get(series, "GradeDate"))
        if published_at is None:
            continue
        key = (firm, published_at)
        if key in seen:
            continue
        seen.add(key)
        changes.append(
            RatingChange(
                firm=firm,
                published_at=published_at,
                action=_text(_series_get(series, "Action")),
                from_grade=_text(_series_get(series, "FromGrade")),
                to_grade=_text(_series_get(series, "ToGrade")),
                target_current=_target(_series_get(series, "currentPriceTarget")),
                target_prior=_target(_series_get(series, "priorPriceTarget")),
            )
        )
    # Newest first, whatever order Yahoo sent (so the [:MAX] cap keeps the most recent).
    changes.sort(key=lambda change: change.published_at, reverse=True)
    return changes


def _series_get(series, key: str):
    try:
        return series.get(key)
    except Exception:  # noqa: BLE001 — a frame quirk must not escape the adapter
        return None


def _to_date(value) -> date | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        converted = pd.to_datetime(value)
    except (TypeError, ValueError):
        return None
    if converted is None or pd.isna(converted):
        return None
    return converted.date()


def _text(value) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def _target(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
