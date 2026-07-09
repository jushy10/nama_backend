"""Interface Adapter: the Alpaca-backed StockDataProvider.

This is the only module that knows Alpaca exists. It translates Alpaca's
SDK models into our Stock entity and Alpaca's failures into domain errors.
Swap data vendors and only this file changes.

SDK: https://alpaca.markets/sdks/python/
"""

import bisect
import logging
from datetime import datetime, timedelta, timezone

from alpaca.common.enums import Sort
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient

from app.stocks.entities import (
    AllTimeHigh,
    Candle,
    CandleSeries,
    MarketIndexPerformance,
    Quote,
    SectorPerformance,
    Stock,
    StockPerformance,
    Timeframe,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import (
    AllTimeHighProvider,
    BulkQuoteProvider,
    CandleProvider,
    MarketOverviewProvider,
    SectorPerformanceProvider,
    StockDataProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)

logger = logging.getLogger(__name__)

# Our vendor-agnostic Timeframe -> Alpaca's (amount, unit).
_TIMEFRAME_MAP: dict[Timeframe, tuple[int, TimeFrameUnit]] = {
    Timeframe.MIN_1: (1, TimeFrameUnit.Minute),
    Timeframe.MIN_5: (5, TimeFrameUnit.Minute),
    Timeframe.MIN_15: (15, TimeFrameUnit.Minute),
    Timeframe.MIN_30: (30, TimeFrameUnit.Minute),
    Timeframe.HOUR_1: (1, TimeFrameUnit.Hour),
    Timeframe.HOUR_4: (4, TimeFrameUnit.Hour),
    Timeframe.DAY_1: (1, TimeFrameUnit.Day),
    Timeframe.WEEK_1: (1, TimeFrameUnit.Week),
    Timeframe.MONTH_1: (1, TimeFrameUnit.Month),
}

# Hard cap on candles per response. Also Alpaca's max page size. We ask Alpaca
# to sort newest-first and cap at this many, so when a window holds more bars
# than the cap it's the *most recent* ones that survive (a chart wants recent
# data); we then reverse back to chronological order before returning.
_MAX_CANDLES = 10_000

# Market sectors read through their SPDR Select Sector ETF. Sector indices
# aren't directly tradable, so the ETF that tracks each sector stands in for it
# — the standard proxy for reading a sector's move on the day.
_SECTOR_ETFS: dict[str, str] = {
    "XLK": "Technology",
    "XLV": "Health Care",
    "XLF": "Financials",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
}

# The headline US indices, read through the tradable ETF that tracks each one —
# the standard proxy, exactly like the sector board (indices aren't tradable, so
# the ETF stands in). SPY tracks the S&P 500; QQQ tracks the Nasdaq-100 (the
# growth-heavy index colloquially "the Nasdaq"). Insertion order is the board's
# order: broad market first.
_INDEX_ETFS: dict[str, str] = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq",
}


class AlpacaStockDataProvider(
    StockDataProvider,
    StockQuoteProvider,
    BulkQuoteProvider,
    StockPerformanceProvider,
    AllTimeHighProvider,
    CandleProvider,
    SectorPerformanceProvider,
    MarketOverviewProvider,
):
    """Fetches stock data from Alpaca and maps it onto the Stock entity.

    Also derives trailing-window performance and the all-time high from daily
    bars, and historical OHLC candles for charting.
    """

    # Symbols per batched snapshot request (get_quotes). Alpaca rejects an oversized
    # multi-symbol snapshot: in prod a 200-symbol chunk failed outright while a
    # ~100-symbol board went through, so the S&P 500 (~500 names) came back entirely
    # uncoloured. Cap well under that — a ~500-name map is then ~5 chunks — and pair it
    # with per-chunk best-effort below so one rejected chunk can never blank the board.
    _SNAPSHOT_CHUNK = 100

    # Lookback long enough to cover the 1-year window with margin for
    # weekends/holidays, so a bar exists at or before each target date.
    _PERFORMANCE_LOOKBACK_DAYS = 400

    # Floor for the all-time-high history scan. Alpaca's market data begins
    # ~2016; this sits well before that so the scan covers everything the feed
    # carries without hardcoding the exact start. How far back the data really
    # reaches is reported back via AllTimeHigh.since (the earliest bar returned),
    # so a caller can see the bound on "all-time".
    _HISTORY_START = datetime(2000, 1, 1, tzinfo=timezone.utc)

    # Trailing-window returns must read the *consolidated* close, not one venue's.
    # IEX is a single exchange (~2.5% of volume): its daily close isn't the
    # official closing print and its history is gappy, which skews the base price
    # a window anchors on (the 1-year base most of all). So historical bars come
    # from SIP (full market coverage + true close). The free plan allows SIP for
    # history as long as the query ends >15 min in the past, so `end` is held
    # back by a small margin. Split-adjusted (not dividend) keeps these as
    # price-return figures, matching public charts and the candle endpoint.
    _HISTORICAL_FEED = DataFeed.SIP
    _SIP_FREE_DELAY = timedelta(minutes=16)

    # Trailing windows as day-count offsets from the latest bar. Months are
    # approximated in days (fine for a performance indicator); YTD is handled
    # separately against the previous year's final close.
    _WINDOW_DAYS = {
        "one_week": 7,
        "one_month": 30,
        "three_month": 91,
        "six_month": 182,
        "one_year": 365,
    }

    # Alpaca's SIP history occasionally carries a "bad tick": a single OHLC field
    # is a garbage value while the rest of the bar is normal. Observed: SPY's
    # daily bar for 2026-02-02 came back with low=$69 against an open/close near
    # $690, which drew a ~90% downward wick on the chart. The bar still satisfies
    # the low <= open/close <= high ordering, so nothing structural flags it —
    # only an outlier check does. No liquid security prints a wick this far from
    # its body (a real 50% intraday move drags the open/close with it, it doesn't
    # leave a lone spike), so a low/high stranded past this fraction of the body
    # is treated as corrupt and clamped back to the body. Deliberately loose:
    # real moves — even flash-crash wicks — stay well inside it and pass through.
    _MAX_WICK_FRACTION = 0.5

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        feed: DataFeed = DataFeed.IEX,  # free plan -> IEX
        paper: bool = True,
    ) -> None:
        self._data = StockHistoricalDataClient(api_key, secret_key)
        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._feed = feed

    def get_stock(self, symbol: str) -> Stock:
        snapshot = self._fetch_snapshot(symbol)
        name, exchange = self._fetch_asset_metadata(symbol)
        return self._to_entity(symbol, snapshot, name, exchange)

    def get_quote(self, symbol: str) -> Quote:
        # Snapshot only: one data call, no asset-metadata lookup. Cheap enough to
        # back a poll-every-few-seconds endpoint.
        return self._to_quote(symbol, self._fetch_snapshot(symbol))

    def get_quotes(self, symbols) -> dict[str, Quote]:
        # The board's live quotes as a handful of chunked snapshot calls (the same call the
        # sector/index boards make, over an arbitrary symbol list). Best-effort at two levels:
        # per symbol — a name the free IEX feed doesn't carry (no snapshot, or no latest_trade)
        # is skipped, so its tile is sized from stored facts and left uncoloured; and per chunk
        # — a chunk Alpaca rejects (a bad/halted symbol, or the batch itself) is logged and
        # skipped, so it can't discard the other chunks' quotes. Only when *every* chunk fails
        # (and nothing was collected) is it a hard feed failure worth surfacing. Dedupe +
        # uppercase once so a repeated/lowercase symbol maps cleanly.
        unique = list(dict.fromkeys(s.upper() for s in symbols if s))
        quotes: dict[str, Quote] = {}
        failures = 0
        for start in range(0, len(unique), self._SNAPSHOT_CHUNK):
            chunk = unique[start : start + self._SNAPSHOT_CHUNK]
            try:
                request = StockSnapshotRequest(symbol_or_symbols=chunk, feed=self._feed)
                snapshots = self._data.get_stock_snapshot(request)
            except APIError as exc:
                # One chunk's failure must not blank the board — skip it and keep the rest.
                failures += 1
                logger.warning("snapshot chunk of %d symbols failed: %s", len(chunk), exc)
                continue
            for symbol in chunk:
                snapshot = snapshots.get(symbol)
                if snapshot is None or snapshot.latest_trade is None:
                    continue
                quotes[symbol] = self._to_quote(symbol, snapshot)
        # Every chunk failed and we got nothing back: a real feed outage, not a sparse board.
        if failures and not quotes:
            raise StockDataUnavailable("quotes", "every snapshot chunk failed")
        return quotes

    def get_performance(self, symbol: str) -> StockPerformance:
        return self._compute_performance(self._fetch_daily_bars(symbol))

    def get_all_time_high(self, symbol: str) -> AllTimeHigh:
        bars = self._fetch_all_daily_bars(symbol)
        if not bars:
            # No history at all -> treat like an unknown symbol so the caller's
            # best-effort wrapper omits the field rather than failing the view.
            raise StockNotFound(symbol)
        return self._to_all_time_high(bars)

    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> CandleSeries:
        amount, unit = _TIMEFRAME_MAP[timeframe]
        # Pick the feed by granularity. Intraday bars (minute/hour) back the
        # short, recent 1D–1M charts, which need IEX's real-time prints and whose
        # windows IEX carries in full. Daily-and-coarser bars drive the long-range
        # charts (out to 10Y), where IEX falls short twice over: its history is
        # gappy and, on this account, only reaches ~mid-2020. SIP has full-market
        # coverage back to Alpaca's ~2016 inception plus the true consolidated
        # close, so the coarse timeframes read from it — the same feed the
        # performance/all-time-high paths use — with `end` held back by the free
        # plan's SIP-history delay so the request stays inside the allowed window.
        intraday = unit in (TimeFrameUnit.Minute, TimeFrameUnit.Hour)
        if intraday:
            feed = self._feed
        else:
            feed = self._HISTORICAL_FEED
            cutoff = datetime.now(timezone.utc) - self._SIP_FREE_DELAY
            end = cutoff if end is None else min(end, cutoff)
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(amount, unit),
                start=start,
                end=end,
                limit=_MAX_CANDLES,
                adjustment=Adjustment.SPLIT,  # keep the line continuous over splits
                sort=Sort.DESC,  # newest-first so the cap keeps recent bars
                feed=feed,
            )
            barset = self._data.get_stock_bars(request)
        except APIError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc

        bars = barset.data.get(symbol, [])
        if not bars:
            raise StockNotFound(symbol)
        # Reverse the newest-first response into chronological (oldest-first)
        # order — the order a chart draws candles left to right.
        candles = tuple(self._to_candle(bar) for bar in reversed(bars))
        return CandleSeries(symbol=symbol, timeframe=timeframe, candles=candles)

    def get_sector_performance(self) -> list[SectorPerformance]:
        # One batched snapshot call covers every sector ETF. A sector missing a
        # quote (e.g. not carried on the IEX free feed) is skipped rather than
        # failing the whole board; only an empty board is a hard error.
        try:
            request = StockSnapshotRequest(
                symbol_or_symbols=list(_SECTOR_ETFS), feed=self._feed
            )
            snapshots = self._data.get_stock_snapshot(request)
        except APIError as exc:
            raise StockDataUnavailable("sectors", str(exc)) from exc

        # Trailing-window performance for every ETF in one more batched call;
        # best-effort, so a failure here leaves the day-change board intact.
        bars_by_symbol = self._fetch_daily_bars_batch(list(_SECTOR_ETFS))

        sectors = [
            self._to_sector(
                symbol,
                sector,
                snapshots[symbol],
                self._compute_performance(bars_by_symbol.get(symbol, [])),
            )
            for symbol, sector in _SECTOR_ETFS.items()
            if snapshots.get(symbol) is not None
            and snapshots[symbol].latest_trade is not None
        ]
        if not sectors:
            raise StockNotFound("sectors")
        return sectors

    def get_market_overview(self) -> list[MarketIndexPerformance]:
        # Same shape as the sector board: one batched snapshot for both index ETFs,
        # one batched bars call for their trailing windows. An index missing a
        # quote is skipped (never fails the board); only an empty board is a hard
        # error, and a bars failure leaves the day-change board intact.
        try:
            request = StockSnapshotRequest(
                symbol_or_symbols=list(_INDEX_ETFS), feed=self._feed
            )
            snapshots = self._data.get_stock_snapshot(request)
        except APIError as exc:
            raise StockDataUnavailable("market", str(exc)) from exc

        bars_by_symbol = self._fetch_daily_bars_batch(list(_INDEX_ETFS))

        indexes = [
            self._to_index(
                symbol,
                name,
                snapshots[symbol],
                self._compute_performance(bars_by_symbol.get(symbol, [])),
            )
            for symbol, name in _INDEX_ETFS.items()
            if snapshots.get(symbol) is not None
            and snapshots[symbol].latest_trade is not None
        ]
        if not indexes:
            raise StockNotFound("market")
        return indexes

    # --- Alpaca calls (thin and isolated) ---

    def _fetch_snapshot(self, symbol: str):
        try:
            request = StockSnapshotRequest(symbol_or_symbols=symbol, feed=self._feed)
            snapshots = self._data.get_stock_snapshot(request)
        except APIError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc

        snapshot = snapshots.get(symbol)
        if snapshot is None or snapshot.latest_trade is None:
            raise StockNotFound(symbol)
        return snapshot

    def _fetch_asset_metadata(self, symbol: str) -> tuple[str | None, str | None]:
        """Company name + listing exchange. Best-effort; never fatal."""
        try:
            asset = self._trading.get_asset(symbol)
        except APIError:
            return None, None
        exchange = asset.exchange.value if asset.exchange else None
        return asset.name, exchange

    def _fetch_daily_bars(self, symbol: str):
        """Daily bars over the lookback window (oldest first)."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=self._PERFORMANCE_LOOKBACK_DAYS)
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start,
                end=now - self._SIP_FREE_DELAY,
                adjustment=Adjustment.SPLIT,
                feed=self._HISTORICAL_FEED,
            )
            barset = self._data.get_stock_bars(request)
        except APIError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        return barset.data.get(symbol, [])

    def _fetch_all_daily_bars(self, symbol: str):
        """Every daily bar the feed carries for the symbol, for the all-time high.

        Reads the SIP feed (full-market coverage and true intraday highs, the
        same consolidated history the performance windows use) split-adjusted, so
        old highs stay comparable to today's split-adjusted price. ``end`` is held
        back from now by the SIP-on-free delay, like the performance fetch, and
        ``start`` reaches back past Alpaca's data floor to capture all of it.
        """
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=self._HISTORY_START,
                end=datetime.now(timezone.utc) - self._SIP_FREE_DELAY,
                adjustment=Adjustment.SPLIT,
                feed=self._HISTORICAL_FEED,
            )
            barset = self._data.get_stock_bars(request)
        except APIError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        return barset.data.get(symbol, [])

    def _fetch_daily_bars_batch(self, symbols: list[str]) -> dict[str, list]:
        """Daily bars over the lookback for several symbols in one request.

        Best-effort: on failure returns an empty map so callers can still serve
        a snapshot-only view without trailing-window performance.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=self._PERFORMANCE_LOOKBACK_DAYS)
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=start,
                end=now - self._SIP_FREE_DELAY,
                adjustment=Adjustment.SPLIT,
                feed=self._HISTORICAL_FEED,
            )
            barset = self._data.get_stock_bars(request)
        except APIError:
            return {}
        return barset.data

    # --- Mapping: Alpaca SDK models -> domain entity ---

    @classmethod
    def _to_candle(cls, bar) -> Candle:
        # Repair an implausible wick (a bad tick from the feed) before it reaches
        # the chart, leaving the rest of the bar untouched. See _repair_bad_tick.
        high, low = cls._repair_bad_tick(bar.open, bar.high, bar.low, bar.close)
        return Candle(
            timestamp=bar.timestamp,
            open=bar.open,
            high=high,
            low=low,
            close=bar.close,
            volume=int(bar.volume) if bar.volume is not None else None,
        )

    @classmethod
    def _repair_bad_tick(
        cls, open_: float, high: float, low: float, close: float
    ) -> tuple[float, float]:
        """Clamp an implausible wick (a bad tick) back to the candle body.

        Returns the repaired ``(high, low)``. A ``low`` stranded more than
        ``_MAX_WICK_FRACTION`` below the body — or a ``high`` that far above it —
        is a corrupt print rather than a real move, so it's pulled in to the body
        edge; a clean bar passes through unchanged.
        """
        body_low = min(open_, close)
        body_high = max(open_, close)
        if body_low > 0 and low < body_low * (1 - cls._MAX_WICK_FRACTION):
            low = body_low
        if body_high > 0 and high > body_high * (1 + cls._MAX_WICK_FRACTION):
            high = body_high
        return high, low

    @staticmethod
    def _to_quote(symbol, snapshot) -> Quote:
        # Same fields the Stock mapping reads, minus everything that needs a
        # second call (name/exchange) or the daily bar (open/high/low/volume).
        trade = snapshot.latest_trade
        quote = snapshot.latest_quote
        prev = snapshot.previous_daily_bar
        return Quote(
            symbol=symbol,
            price=trade.price,
            previous_close=prev.close if prev else None,
            bid=quote.bid_price if quote else None,
            ask=quote.ask_price if quote else None,
            as_of=trade.timestamp,
        )

    @staticmethod
    def _to_entity(symbol, snapshot, name, exchange) -> Stock:
        trade = snapshot.latest_trade
        quote = snapshot.latest_quote
        daily = snapshot.daily_bar
        prev = snapshot.previous_daily_bar
        return Stock(
            symbol=symbol,
            name=name,
            exchange=exchange,
            price=trade.price,
            open=daily.open if daily else None,
            high=daily.high if daily else None,
            low=daily.low if daily else None,
            previous_close=prev.close if prev else None,
            volume=int(daily.volume) if daily and daily.volume is not None else None,
            bid=quote.bid_price if quote else None,
            ask=quote.ask_price if quote else None,
            as_of=trade.timestamp,
        )

    @staticmethod
    def _to_sector(symbol, sector, snapshot, performance) -> SectorPerformance:
        # Day's move = latest trade vs the previous daily close, mirroring the
        # Stock entity's own change rule; `performance` carries the trailing
        # windows (1w/1m/3m/6m/ytd/1y).
        trade = snapshot.latest_trade
        prev = snapshot.previous_daily_bar
        return SectorPerformance(
            sector=sector,
            symbol=symbol,
            price=trade.price,
            previous_close=prev.close if prev else None,
            as_of=trade.timestamp,
            performance=performance,
        )

    @staticmethod
    def _to_index(symbol, name, snapshot, performance) -> MarketIndexPerformance:
        # Same rules as _to_sector: day move = latest trade vs the previous daily
        # close; `performance` carries the trailing windows (1w/1m/…/1y).
        trade = snapshot.latest_trade
        prev = snapshot.previous_daily_bar
        return MarketIndexPerformance(
            name=name,
            symbol=symbol,
            price=trade.price,
            previous_close=prev.close if prev else None,
            as_of=trade.timestamp,
            performance=performance,
        )

    # --- Mapping: Alpaca bars -> all-time high ---

    @staticmethod
    def _to_all_time_high(bars) -> AllTimeHigh:
        """Highest intraday high across the history, with when and how far back.

        ``since`` is the earliest bar's date — the bound on "all-time," since the
        feed's history may not reach the stock's listing.
        """
        peak = max(bars, key=lambda bar: bar.high)
        earliest = min(bar.timestamp for bar in bars)
        return AllTimeHigh(
            price=peak.high,
            reached_on=peak.timestamp.date(),
            since=earliest.date(),
        )

    # --- Mapping: Alpaca bars -> performance windows ---

    @classmethod
    def _compute_performance(cls, bars) -> StockPerformance:
        """Percent change of the latest close vs the close starting each window."""
        if not bars:
            return StockPerformance(None, None, None, None, None, None)
        bars = sorted(bars, key=lambda b: b.timestamp)  # ascending; defensive
        dates = [b.timestamp.date() for b in bars]
        current = bars[-1].close
        anchor = dates[-1]

        def pct_since(target_date) -> float | None:
            idx = bisect.bisect_right(dates, target_date) - 1  # last bar <= target
            if idx < 0:
                return None
            base = bars[idx].close
            return round((current - base) / base * 100, 2) if base else None

        windows = {
            name: pct_since(anchor - timedelta(days=days))
            for name, days in cls._WINDOW_DAYS.items()
        }
        return StockPerformance(
            one_week=windows["one_week"],
            one_month=windows["one_month"],
            three_month=windows["three_month"],
            six_month=windows["six_month"],
            ytd=cls._ytd(bars, dates, current, anchor.year),
            one_year=windows["one_year"],
        )

    @staticmethod
    def _ytd(bars, dates, current, anchor_year) -> float | None:
        """Percent change vs the previous year's final close."""
        for i in range(len(bars) - 1, -1, -1):
            if dates[i].year < anchor_year:  # most recent bar before this year
                base = bars[i].close
                return round((current - base) / base * 100, 2) if base else None
        return None
