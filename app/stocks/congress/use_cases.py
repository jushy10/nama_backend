"""Application use cases for the Congressional-trades slice.

Three actions, all pure orchestration over the ports so they run offline in tests against
hand-written fakes and know nothing of the stock-watcher datasets, HTTP, or SQLAlchemy:

- ``GetCongressTrades`` — the per-ticker read path. Normalizes the symbol and serves the stored
  activity **DB-only** through the repository (a miss reads empty; a DB hiccup degrades to empty).
- ``GetCongressActivity`` — the market-wide read path. Serves a windowed page of the whole market's
  recent trades, also DB-only and best-effort.
- ``SyncCongressTrades`` — the out-of-band refresh. Fetches the whole market-wide feed **once**,
  groups it by ticker, then walks the anchor least-recently-refreshed first (un-cached first, so it
  also *seeds* new coverage) and stores each stock's trades. Invoked by the weekly cron.

The read is deliberately DB-only (no live fall-through) — the feed is a multi-megabyte download, so
a user request must never trigger it; keeping the store current is entirely the cron's job. The
sync is *bulk* (one fetch covers every ticker), unlike the per-symbol earnings/insider syncs, so
there are no per-symbol network round-trips — the stalest-first walk just decides the order stocks
are distributed to (and bounds a partial ``limit`` run).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from app.stocks.congress.entities import (
    CongressActivity,
    CongressLeaderboard,
    CongressMarketActivity,
    CongressMetric,
    CongressTrade,
    build_leaderboard,
)
from app.stocks.congress.ports import CongressTradesSource
from app.stocks.congress.repository import CongressTradesRepository
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.progress import iter_with_progress

logger = logging.getLogger(__name__)

# A ticker is 1–5 letters, optionally with a single class suffix (BRK-B). The universe stores the
# suffix with a hyphen, so a dotted input (BRK.B) is folded onto it — a touch more permissive than
# the ticker card's alpha-only guard so a class-share name still resolves.
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker, fold a dotted class suffix onto the stored hyphen form, and
    reject obvious junk — once, at the edge of the use case, so every layer below sees a clean
    symbol."""
    normalized = (symbol or "").strip().upper().replace(".", "-")
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not _TICKER_RE.match(normalized):
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


def _clamp_offset(offset: int | None) -> int:
    return max(0, offset or 0)


class GetCongressTrades:
    """Use case: a stock's recent Congressional trades by its symbol — DB-only, best-effort.

    A stock Congress hasn't traded (or the cron hasn't seeded yet) yields an empty activity rather
    than an error, so the endpoint can present an empty result instead of a 404. ``limit`` /
    ``offset`` page the trade list; the ``summary`` always reflects the *full* stored set, not the
    page, so a client sees the true net buy-vs-sell regardless of where it is in the list.
    """

    def __init__(self, repository: CongressTradesRepository) -> None:
        self._repository = repository

    def execute(self, symbol: str) -> CongressActivity:
        normalized = _normalize_symbol(symbol)
        try:
            stored = self._repository.get(normalized)
        except Exception:  # noqa: BLE001 — best-effort feed; a DB hiccup reads empty, never 500s
            logger.warning(
                "congress trades cache read failed for %s", normalized, exc_info=True
            )
            stored = None
        return stored if stored is not None else CongressActivity(normalized)


# window token -> number of days (None = no window / all history). The market board accepts these.
_WINDOWS: dict[str, int | None] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "180d": 180,
    "1y": 365,
    "all": None,
}
_DEFAULT_WINDOW = "30d"


def parse_window(window: str | None) -> int | None:
    """Map a window token (``"30d"``, ``"1y"``, ``"all"``) to a number of days (``None`` = all
    history). An unknown token is a ``ValueError`` the endpoint maps to a 400."""
    token = (window or _DEFAULT_WINDOW).strip().lower()
    if token not in _WINDOWS:
        allowed = ", ".join(_WINDOWS)
        raise ValueError(f"Unknown window '{window}'. Use one of: {allowed}.")
    return _WINDOWS[token]


class GetCongressActivity:
    """Use case: a window of the whole market's recent Congressional trades — DB-only, best-effort.

    The market board. Serves a paginated, newest-first page of trades across every stock, windowed
    to the last ``window_days`` (``None`` = all history). Best-effort: a DB hiccup reads as an empty
    board, never a 500.
    """

    def __init__(self, repository: CongressTradesRepository, *, today=None) -> None:
        self._repository = repository
        # Injectable clock keeps the window's cutoff deterministic in tests.
        self._today = today or date.today

    def execute(
        self, *, window_days: int | None, limit: int, offset: int
    ) -> CongressMarketActivity:
        since = None if window_days is None else self._today() - timedelta(days=window_days)
        offset = _clamp_offset(offset)
        try:
            trades, total = self._repository.recent_market_activity(
                since=since, limit=limit, offset=offset
            )
        except Exception:  # noqa: BLE001 — best-effort board; a DB hiccup reads empty, never 500s
            logger.warning("congress market activity read failed", exc_info=True)
            trades, total = [], 0
        return CongressMarketActivity(
            trades=tuple(trades), total=total, window_days=window_days
        )


# The metrics the leaderboard ranks on (see ``CongressMetric``). ``members`` is the default — the
# breadth of Congressional interest reads as the truest "most attention" signal.
_METRICS: tuple[CongressMetric, ...] = ("members", "trades", "value")
_DEFAULT_METRIC: CongressMetric = "members"


def parse_metric(metric: str | None) -> CongressMetric:
    """Validate the leaderboard ranking metric token (``"members"`` / ``"trades"`` / ``"value"``),
    defaulting to ``members``. An unknown token is a ``ValueError`` the endpoint maps to a 400."""
    token = (metric or _DEFAULT_METRIC).strip().lower()
    if token not in _METRICS:
        allowed = ", ".join(_METRICS)
        raise ValueError(f"Unknown metric '{metric}'. Use one of: {allowed}.")
    return token  # type: ignore[return-value]


class GetCongressLeaderboard:
    """Use case: the stocks getting the most Congressional attention over a window — DB-only,
    best-effort.

    Reads the whole window of market-wide trades once, folds it by ticker into a ranked board (by
    distinct members, disclosure count, or estimated dollars moved) and cuts the top ``limit``. Like
    the other reads it's DB-only — the weekly cron keeps the store warm — and best-effort: a DB
    hiccup reads as an empty board, never a 500.
    """

    def __init__(self, repository: CongressTradesRepository, *, today=None) -> None:
        self._repository = repository
        # Injectable clock keeps the window's cutoff deterministic in tests.
        self._today = today or date.today

    def execute(
        self, *, window_days: int | None, metric: CongressMetric, limit: int
    ) -> CongressLeaderboard:
        since = None if window_days is None else self._today() - timedelta(days=window_days)
        try:
            trades = self._repository.market_trades_in_window(since=since)
        except Exception:  # noqa: BLE001 — best-effort board; a DB hiccup reads empty, never 500s
            logger.warning("congress leaderboard read failed", exc_info=True)
            trades = []
        entries = build_leaderboard(trades, metric=metric, limit=limit)
        # Distinct stocks Congress touched in the window, before the top-N cut — so the client can
        # say "showing N of M".
        total_stocks = len({trade.ticker for trade in trades})
        return CongressLeaderboard(
            entries=entries,
            metric=metric,
            window_days=window_days,
            total_stocks=total_stocks,
        )


@dataclass(frozen=True)
class CongressSyncReport:
    """The outcome of one refresh run: how many trades were fetched from the source, how many stocks
    were stored (seeded/refreshed), how many failed a store, and the per-run cap (``None`` when the
    run was uncapped)."""

    fetched: int
    stored: int
    failed: int
    limit: int | None


class SyncCongressTrades:
    """Refresh stored Congressional trades from the live source, distributing one bulk fetch across
    the anchor most-stale-first — and **seed** stocks not yet cached (never-fetched anchor stocks
    come first)."""

    def __init__(
        self,
        source: CongressTradesSource,
        repository: CongressTradesRepository,
    ) -> None:
        self._source = source
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> CongressSyncReport:
        """Fetch the whole market-wide feed once, group it by ticker, then store each anchor stock's
        trades most-stale-first. ``limit`` caps how many anchor stocks are visited this run;
        ``None`` (the default) visits every stock. A total source outage propagates (the task exits
        non-zero); a single stock's store failure is counted and the run continues.

        Raises:
            StockDataUnavailable: every underlying feed failed — nothing to distribute this run.
        """
        effective = None if limit is None else max(1, limit)
        all_trades = self._source.fetch_recent_trades()  # raises only on a total-source outage
        by_ticker: dict[str, list[CongressTrade]] = defaultdict(list)
        for trade in all_trades:
            by_ticker[trade.ticker].append(trade)

        stored = 0
        failed = 0
        targets = self._repository.refresh_targets(effective)
        for target in iter_with_progress(targets, logger=logger, label="congress sync"):
            trades = by_ticker.get(target.symbol)
            if not trades:
                # An anchor stock Congress hasn't traded — nothing to store (not a failure). Most
                # anchor stocks fall here; the bulk fetch already covered them at zero extra cost.
                continue
            activity = CongressActivity(target.symbol, tuple(trades))
            try:
                self._repository.upsert(target.symbol, target.name, activity)
            except (StockNotFound, StockDataUnavailable):
                failed += 1
                continue
            stored += 1
        return CongressSyncReport(
            fetched=len(all_trades), stored=stored, failed=failed, limit=effective
        )
