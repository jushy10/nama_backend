"""Interface Adapter: the Alpaca-backed StockDataProvider.

This is the only module that knows Alpaca exists. It translates Alpaca's
SDK models into our Stock entity and Alpaca's failures into domain errors.
Swap data vendors and only this file changes.

SDK: https://alpaca.markets/sdks/python/
"""

import bisect
from datetime import datetime, timedelta, timezone

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

from app.stocks.entities import Stock, StockPerformance
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import StockDataProvider, StockPerformanceProvider


class AlpacaStockDataProvider(StockDataProvider, StockPerformanceProvider):
    """Fetches stock data from Alpaca and maps it onto the Stock entity.

    Also derives trailing-window performance from daily price bars.
    """

    # Lookback long enough to cover the 1-year window with margin for
    # weekends/holidays, so a bar exists at or before each target date.
    _PERFORMANCE_LOOKBACK_DAYS = 400

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

    def get_performance(self, symbol: str) -> StockPerformance:
        return self._compute_performance(self._fetch_daily_bars(symbol))

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
        start = datetime.now(timezone.utc) - timedelta(
            days=self._PERFORMANCE_LOOKBACK_DAYS
        )
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start,
                feed=self._feed,
            )
            barset = self._data.get_stock_bars(request)
        except APIError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        return barset.data.get(symbol, [])

    # --- Mapping: Alpaca SDK models -> domain entity ---

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
