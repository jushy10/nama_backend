"""Interface Adapter: Congressional stock trades from the community "stock watcher" datasets.

The only module that knows which dataset backs the Congressional-trades slice; swap it for another
``CongressTradesSource`` and only this file changes. It fetches the House and Senate disclosure
feeds — keyless, community-maintained JSON mirrors of the trades members file under the STOCK Act —
and translates their rows into ``CongressTrade`` entities, mapping each vendor's failures into our
domain exceptions.

**Keyless**, like the SEC sources and unlike the paid Congressional-trade APIs (Quiver, Capitol
Trades' API): these are public GitHub-hosted JSON files, so there's no credential to gate on and no
per-IP block to pace around. Its only ask is basic courtesy — a descriptive ``User-Agent`` and not
hammering the host — which the adapter honours.

The two chambers publish **different schemas** (the House feed keys the member as
``representative`` and carries a ``disclosure_date``; the Senate feed keys ``senator`` and carries
only the transaction date, splitting a sale into ``"Sale (Full)"`` / ``"Sale (Partial)"``), so each
feed is described by a small ``_Feed`` profile and normalized onto one ``CongressTrade`` shape.
Neither feed carries party, so ``party`` is left ``None`` — a best-effort field.

Best-effort **per feed**: a single chamber's feed failing (transport / bad body) is logged and
skipped so the other chamber still syncs; only when *every* feed fails does ``fetch_recent_trades``
raise ``StockDataUnavailable``. ``_parse_feed`` is a pure function the tests drive on canned rows;
``_http`` is the fake seam.

> Data note: the House feed (``TattooedHead/house-stock-watcher-data``) is actively maintained and
> current; the classic Senate mirror (``timothycarambat/senate-stock-watcher-data``) is a keyless
> historical archive. Because this is the sole vendor-aware module, pointing either chamber at a
> fresher mirror later is a one-line URL change here — nothing downstream moves.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date

import httpx

from app.stocks.congress.entities import (
    EXCHANGE,
    OTHER,
    PURCHASE,
    SALE,
    CongressTrade,
)
from app.stocks.congress.ports import CongressTradesSource
from app.stocks.exceptions import StockDataUnavailable

logger = logging.getLogger(__name__)

# Basic courtesy for the public GitHub-raw hosts (they don't require it, but it's good manners and
# makes our traffic identifiable).
_USER_AGENT = "nama-backend/1.0 (congress-trades sync; +https://namainsights.com)"


@dataclass(frozen=True)
class _Feed:
    """One chamber's feed profile: where to fetch it and how its rows are keyed.

    Both feeds share ``ticker`` / ``asset_description`` / ``type`` / ``amount`` / ``owner`` /
    ``transaction_date``; only the member key, the disclosure-date key, and the source-link key
    differ between chambers.
    """

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
    """Reads recent Congressional trades from the keyless House / Senate stock-watcher feeds."""

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

    # ── HTTP plumbing ─────────────────────────────────────────────────────────────────────

    def _get_json(self, url: str, label: str) -> list:
        """GET ``url`` and parse it as a JSON array (the feeds are a flat list of trade rows). A
        transport failure, a non-200, or a non-JSON/non-array body is a source failure."""
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
        """Sleep just enough to keep successive downloads at/under the configured spacing — a no-op
        when the interval is 0 (the test default)."""
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()


# ── parsing (pure — no HTTP, exercised directly by the tests) ─────────────────────────────


def _parse_feed(payload: list, feed: _Feed) -> list[CongressTrade]:
    """Translate one chamber feed's rows into ``CongressTrade`` entities.

    Each row is best-effort: a row with no member or no recognisable stock ticker is dropped
    (nothing to key or read it by); everything past those two is normalized and left ``None`` when
    absent. A malformed row (a non-dict entry) is skipped rather than raising, so one bad record
    can't sink the whole feed.
    """
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
    """Fold both feeds' transaction wording onto the entity's small vocabulary: ``Purchase`` /
    ``Sale`` (the Senate's ``"Sale (Full)"`` / ``"Sale (Partial)"`` both collapse here) /
    ``Exchange`` / ``Other`` (receipts, gifts, and anything unrecognised)."""
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
    """A stock ticker normalized to the anchor's convention (upper-case, a dotted class suffix
    folded onto the stored hyphen form: ``BRK.B`` -> ``BRK-B``), or ``None`` for a placeholder
    (``"--"``), a blank, or anything that isn't a plain 1–5 letter ticker (options, bonds, crypto
    and other non-equity disclosures the feeds also carry)."""
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
    """A stripped string, or ``None`` for a blank / placeholder (``"--"``) / non-string value."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped == "--":
        return None
    return stripped


def _clip(text: str | None, length: int) -> str | None:
    """Bound a free-text field to its DB column width so an outlier can't overflow on Postgres.
    ``None`` passes through unchanged."""
    if text is None:
        return None
    return text[:length]


def _parse_date(value: object) -> date | None:
    """Parse the feeds' ``MM/DD/YYYY`` date (or an ISO ``YYYY-MM-DD``), returning ``None`` for a
    blank / placeholder / unparseable value. Implausible years (a garbled ``0009/06/08`` some raw
    rows carry) are rejected as ``None`` so they can't poison the ordering."""
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
    """``datetime.strptime`` narrowed to a ``date`` (kept tiny so ``_parse_date`` stays readable)."""
    from datetime import datetime

    return datetime.strptime(text, fmt).date()
