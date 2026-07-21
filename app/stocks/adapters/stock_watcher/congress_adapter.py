from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date

import httpx

from app.stocks.company.congress.entities import (
    EXCHANGE,
    OTHER,
    PURCHASE,
    SALE,
    CongressTrade,
)
from app.stocks.company.congress.ports import CongressTradesSource
from app.stocks.exceptions import StockDataUnavailable

logger = logging.getLogger(__name__)

# Basic courtesy for the public GitHub-raw hosts (they don't require it, but it's good manners and
# makes our traffic identifiable).
_USER_AGENT = "nama-backend/1.0 (congress-trades sync; +https://namainsights.com)"


@dataclass(frozen=True)
class _Feed:
    chamber: str
    url: str
    member_key: str
    disclosure_key: str | None  # House carries one; the Senate archive doesn't
    source_key: str  # "source_url" (House) / "ptr_link" (Senate)


_HOUSE_FEED = _Feed(
    chamber="House",
    url="https://raw.githubusercontent.com/TattooedHead/house-stock-watcher-data/main/data/all_transactions.json",
    member_key="representative",
    disclosure_key="disclosure_date",
    source_key="source_url",
)

_SENATE_FEED = _Feed(
    chamber="Senate",
    url="https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json",
    member_key="senator",
    disclosure_key=None,
    source_key="ptr_link",
)

_DEFAULT_FEEDS = (_HOUSE_FEED, _SENATE_FEED)

# Belt to good manners: a minimum spacing between the (few, large) feed downloads. 0 in the adapter
# default (tests never sleep); the production wiring dials it up a touch.
_DEFAULT_MIN_REQUEST_INTERVAL = 0.0

# Clip the free-text fields to the width of their DB columns so a pathological outlier can never
# overflow the column on Postgres — a swallowed cache write would otherwise silently stop the stock
# from ever caching. Real values are far shorter; this is a guard. (member/amount/owner widths.)
_MEMBER_MAX = 160
_AMOUNT_MAX = 64
_OWNER_MAX = 32
_URL_MAX = 512


class StockWatcherCongressProvider(CongressTradesSource):
    def __init__(
        self,
        feeds: tuple[_Feed, ...] = _DEFAULT_FEEDS,
        *,
        min_request_interval_seconds: float = _DEFAULT_MIN_REQUEST_INTERVAL,
    ) -> None:
        self._feeds = feeds
        self._http = httpx.Client(
            timeout=60.0,  # the House file is ~10 MB
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        )
        self._min_interval = max(0.0, min_request_interval_seconds)
        self._last_request = 0.0

    def fetch_recent_trades(self) -> tuple[CongressTrade, ...]:
        trades: list[CongressTrade] = []
        failures = 0
        for feed in self._feeds:
            try:
                payload = self._get_json(feed.url, feed.chamber)
            except StockDataUnavailable:
                # Best-effort per feed: one chamber down must not sink the other.
                logger.warning(
                    "congress: skipping %s feed (fetch failed)", feed.chamber, exc_info=True
                )
                failures += 1
                continue
            trades.extend(_parse_feed(payload, feed))
        if failures == len(self._feeds):
            # Every feed failed — there's nothing to distribute this run.
            raise StockDataUnavailable(
                "congress", "every Congressional-trades feed failed"
            )
        # Newest activity first (disclosure date, falling back to transaction date), matching the DB
        # repository's serving order so a live-served and cache-served response are consistent.
        trades.sort(key=lambda t: t.activity_date or date.min, reverse=True)
        return tuple(trades)

    def _get_json(self, url: str, label: str) -> list:
        self._pace()
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(label, f"congress feed request failed: {exc}") from exc
        if resp.status_code != 200:
            raise StockDataUnavailable(
                label, f"congress feed returned HTTP {resp.status_code} for {url}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(
                label, f"congress feed returned non-JSON for {url}"
            ) from exc
        return payload if isinstance(payload, list) else []

    def _pace(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()


# ── parsing (pure — no HTTP, exercised directly by the tests) ─────────────────────────────


def _parse_feed(payload: list, feed: _Feed) -> list[CongressTrade]:
    trades: list[CongressTrade] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        member = _clip(_text(raw.get(feed.member_key)), _MEMBER_MAX)
        ticker = _normalize_ticker(raw.get("ticker"))
        if not member or not ticker:
            continue  # can't key or read a trade without a member and a real ticker
        disclosure = (
            _parse_date(raw.get(feed.disclosure_key))
            if feed.disclosure_key
            else None
        )
        trades.append(
            CongressTrade(
                member=member,
                chamber=feed.chamber,
                party=None,  # the keyless feeds don't carry party
                ticker=ticker,
                company_name=_text(raw.get("asset_description")),
                tx_type=_normalize_tx_type(raw.get("type")),
                amount_range=_clip(_text(raw.get("amount")), _AMOUNT_MAX),
                transaction_date=_parse_date(raw.get("transaction_date")),
                disclosure_date=disclosure,
                owner=_clip(_text(raw.get("owner")), _OWNER_MAX),
                source_url=_clip(_text(raw.get(feed.source_key)), _URL_MAX),
            )
        )
    return trades


def _normalize_tx_type(value: object) -> str:
    text = _text(value)
    if not text:
        return OTHER
    lowered = text.lower()
    if lowered.startswith("purchase"):
        return PURCHASE
    if lowered.startswith("sale"):
        return SALE
    if lowered.startswith("exchange"):
        return EXCHANGE
    return OTHER


def _normalize_ticker(value: object) -> str | None:
    text = _text(value)
    if not text or text == "--":
        return None
    ticker = text.upper().replace(".", "-")
    head, _, suffix = ticker.partition("-")
    if not (1 <= len(head) <= 5) or not head.isalpha():
        return None
    if suffix and not (1 <= len(suffix) <= 2 and suffix.isalpha()):
        return None
    return ticker


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped == "--":
        return None
    return stripped


def _clip(text: str | None, length: int) -> str | None:
    if text is None:
        return None
    return text[:length]


def _parse_date(value: object) -> date | None:
    text = _text(value)
    if not text:
        return None
    parsed: date | None = None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed = _strptime_date(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None or parsed.year < 2000 or parsed.year > 2100:
        return None
    return parsed


def _strptime_date(text: str, fmt: str) -> date:
    from datetime import datetime

    return datetime.strptime(text, fmt).date()
