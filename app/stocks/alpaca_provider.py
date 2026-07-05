"""Interface Adapter: the Alpaca-backed StockDataProvider.

This is the only module that knows Alpaca exists. It translates Alpaca's
SDK models into our Stock entity and Alpaca's failures into domain errors.
Swap data vendors and only this file changes.

SDK: https://alpaca.markets/sdks/python/
"""

import bisect
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
    Quote,
    SectorPerformance,
    Stock,
    StockPerformance,
    Timeframe,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import (
    AllTimeHighProvider,
    CandleProvider,
    SectorPerformanceProvider,
    StockDataProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)

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


class AlpacaStockDataProvider(
    StockDataProvider,
    StockQuoteProvider,
    StockPerformanceProvider,
    AllTimeHighProvider,
    CandleProvider,
    SectorPerformanceProvider,
):
    """Fetches stock data from Alpaca and maps it onto the Stock entity.

    Also derives trailing-window performance and the all-time high from daily
    bars, and historical OHLC candles for charting.
    """

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
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(amount, unit),
                start=start,
                end=end,
                limit=_MAX_CANDLES,
                adjustment=Adjustment.SPLIT,  # keep the line continuous over splits
                sort=Sort.DESC,  # newest-first so the cap keeps recent bars
                feed=self._feed,
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

    @staticmethod
    def _to_candle(bar) -> Candle:
        return Candle(
            timestamp=bar.timestamp,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=int(bar.volume) if bar.volume is not None else None,
        )

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
