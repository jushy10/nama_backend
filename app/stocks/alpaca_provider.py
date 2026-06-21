"""Interface Adapter: the Alpaca-backed StockDataProvider.

This is the only module that knows Alpaca exists. It translates Alpaca's
SDK models into our Stock entity and Alpaca's failures into domain errors.
Swap data vendors and only this file changes.

SDK: https://alpaca.markets/sdks/python/
"""

import httpx
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from alpaca.trading.client import TradingClient

from app.stocks.entities import Stock
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import StockDataProvider


class AlpacaStockDataProvider(StockDataProvider):
    """Fetches stock data from Alpaca and maps it onto the Stock entity."""

    # The logo endpoint isn't covered by alpaca-py, so we call it over HTTP.
    _DATA_BASE_URL = "https://data.alpaca.markets/v1beta1"

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
        self._http = httpx.Client(
            base_url=self._DATA_BASE_URL,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
            },
            timeout=10.0,
        )

    def get_stock(self, symbol: str) -> Stock:
        snapshot = self._fetch_snapshot(symbol)
        name, exchange = self._fetch_asset_metadata(symbol)
        return self._to_entity(symbol, snapshot, name, exchange)

    def get_logo(self, symbol: str) -> bytes:
        try:
            resp = self._http.get(f"/logos/{symbol}")
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code == 404:
            raise StockNotFound(symbol)
        if resp.status_code != 200:
            raise StockDataUnavailable(
                symbol, f"logo request failed (HTTP {resp.status_code})"
            )
        return resp.content

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
