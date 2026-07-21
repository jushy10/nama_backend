from __future__ import annotations

import re
from datetime import date

import pandas as pd
import yfinance as yf

from app.stocks.adapters.yfinance import session
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.company.recommendations.entities import (
    AnalystPriceTargets,
    AnalystRecommendations,
    RecommendationTrend,
)
from app.stocks.company.recommendations.interfaces import RecommendationAdapter

# Yahoo's relative month labels: "0m" (this month), "-1m", "-2m", ...
_PERIOD_LABEL = re.compile(r"^(0|-\d+)m$")

# The five stance columns, paired with the entity field each feeds.
_STANCE_COLUMNS = (
    ("strongBuy", "strong_buy"),
    ("buy", "buy"),
    ("hold", "hold"),
    ("sell", "sell"),
    ("strongSell", "strong_sell"),
)


class RecommendationAdapterImpl(RecommendationAdapter):
    def __init__(self, *, ticker_factory=None, today=None) -> None:
        # Injectable so tests supply a fake Ticker (and a fixed "today" for the relative
        # month labels) instead of reaching Yahoo; defaults to the real thing.
        self._ticker_factory = ticker_factory or yf.Ticker
        self._today = today or date.today

    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        try:
            # One Ticker reused for both reads below. An empty frame is how yfinance
            # surfaces a swallowed crumb 401, so retry once with a fresh crumb; genuine
            # no-coverage just comes back empty after that. Ticker construction stays inside
            # the try so a construction failure also becomes a domain error.
            ticker = self._ticker_factory(symbol)
            frame = session.call(
                lambda: ticker.recommendations,
                is_empty=session.frame_is_empty,
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance recommendations failed ({exc})"
            ) from exc
        trends = _parse_trends(frame, self._today())
        # Price targets are best-effort enrichment riding on the run: a separate, cheap Yahoo
        # read whose failure must not sink the trends, so it never raises (returns None).
        price_targets = _fetch_price_targets(ticker)
        return AnalystRecommendations(
            symbol=symbol, trends=tuple(trends), price_targets=price_targets
        )


def _fetch_price_targets(ticker) -> AnalystPriceTargets | None:
    try:
        raw = session.call(lambda: ticker.analyst_price_targets)
    except Exception:  # noqa: BLE001 — best-effort enrichment: a failure just omits targets
        return None
    if not isinstance(raw, dict) or not raw:
        return None
    targets = AnalystPriceTargets(
        mean=_target(raw.get("mean")),
        high=_target(raw.get("high")),
        low=_target(raw.get("low")),
        median=_target(raw.get("median")),
    )
    return None if targets.is_empty else targets


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


def _parse_trends(frame, today: date) -> list[RecommendationTrend]:
    if frame is None or getattr(frame, "empty", True):
        return []
    try:
        pairs = list(frame.iterrows())
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return []
    seen: set[date] = set()
    trends: list[RecommendationTrend] = []
    for index, series in pairs:
        # Current yfinance carries the label in a "period" column; older versions used
        # the index. Prefer the column, fall back to the index.
        offset = _month_offset(_series_get(series, "period"))
        if offset is None:
            offset = _month_offset(index)
        if offset is None:
            continue
        period = _month_start(today, offset)
        if period in seen:
            continue
        seen.add(period)
        counts = {
            field: _count(_series_get(series, column))
            for column, field in _STANCE_COLUMNS
        }
        trends.append(RecommendationTrend(period=period, **counts))
    trends.sort(key=lambda t: t.period, reverse=True)  # newest first, whatever Yahoo sent
    return trends


def _month_offset(label) -> int | None:
    if not isinstance(label, str):
        return None
    match = _PERIOD_LABEL.match(label.strip())
    if match is None:
        return None
    return int(match.group(1))


def _month_start(today: date, offset: int) -> date:
    months = today.year * 12 + (today.month - 1) + offset
    return date(months // 12, months % 12 + 1, 1)


def _series_get(series, key: str):
    try:
        return series.get(key)
    except Exception:  # noqa: BLE001 — a frame quirk must not escape the adapter
        return None


def _count(value) -> int:
    if value is None:
        return 0
    try:
        if pd.isna(value):
            return 0
    except (TypeError, ValueError):
        pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
