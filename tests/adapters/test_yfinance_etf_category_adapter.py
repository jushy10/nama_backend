"""Tests for the yfinance ETF category adapter (category from Ticker.info).

Offline: a fake Ticker is injected through the adapter's ``ticker_factory`` seam, so this
exercises the mapping (Yahoo display label → snake_case slug) and the vendor-error and
missing-data handling without touching Yahoo.
"""

import pytest

from app.stocks.adapters.yfinance_etf_category_adapter import (
    YfinanceEtfCategoryProvider,
)
from app.stocks.etfs.entities import EtfClassification
from app.stocks.exceptions import StockDataUnavailable


class _FakeTicker:
    """A stand-in for ``yf.Ticker`` exposing a canned ``.info`` (or raising on access)."""

    def __init__(self, info, *, error=None) -> None:
        self._info = info
        self._error = error

    @property
    def info(self):
        if self._error is not None:
            raise self._error
        return self._info


def _provider(ticker: _FakeTicker) -> YfinanceEtfCategoryProvider:
    return YfinanceEtfCategoryProvider(ticker_factory=lambda symbol: ticker)


def test_maps_the_category_label_to_a_snake_case_slug():
    provider = _provider(_FakeTicker({"category": "Large Growth"}))
    assert provider.get_category("QQQ") == EtfClassification(category="large_growth")


def test_slug_collapses_punctuation_and_whitespace():
    provider = _provider(_FakeTicker({"category": "Commodities Focused"}))
    assert provider.get_category("GLD") == EtfClassification(category="commodities_focused")


def test_missing_or_non_string_category_yields_none():
    # An empty info (a fund Yahoo doesn't categorise), a non-string, and a blank all collapse to
    # None rather than an error.
    assert _provider(_FakeTicker({})).get_category("X") == EtfClassification()
    assert _provider(_FakeTicker({"category": 123})).get_category("X") == EtfClassification()
    assert _provider(_FakeTicker({"category": "  "})).get_category("X") == EtfClassification()


def test_none_info_yields_an_empty_classification():
    # yfinance can hand back a falsy .info; the adapter treats it as "no category".
    assert _provider(_FakeTicker(None)).get_category("X") == EtfClassification()


def test_info_failure_becomes_a_domain_error():
    provider = _provider(_FakeTicker(None, error=RuntimeError("429 Too Many Requests")))
    with pytest.raises(StockDataUnavailable):
        provider.get_category("SPY")
