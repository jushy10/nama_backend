"""Tests for the yfinance classification adapter (sector + industry from Ticker.info).

Offline: a fake Ticker is injected through the adapter's ``ticker_factory`` seam, so this
exercises the mapping (Yahoo display labels → snake_case slugs) and the vendor-error and
missing-data handling without touching Yahoo.
"""

import pytest

from app.stocks.adapters.yfinance_classification_adapter import (
    YfinanceClassificationProvider,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import CompanyClassification


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


def _provider(ticker: _FakeTicker) -> YfinanceClassificationProvider:
    return YfinanceClassificationProvider(ticker_factory=lambda symbol: ticker)


def test_maps_display_labels_to_snake_case_slugs():
    provider = _provider(
        _FakeTicker({"sector": "Technology", "industry": "Consumer Electronics"})
    )
    assert provider.get_classification("AAPL") == CompanyClassification(
        sector="technology", industry="consumer_electronics"
    )


def test_slugs_collapse_punctuation_and_whitespace():
    provider = _provider(
        _FakeTicker({"sector": "Financial Services", "industry": "Insurance—Life & Health"})
    )
    assert provider.get_classification("MET") == CompanyClassification(
        sector="financial_services", industry="insurance_life_health"
    )


def test_missing_or_non_string_fields_yield_none():
    # An empty info (a symbol Yahoo doesn't classify), a non-string, and a blank string all
    # collapse to None rather than an error.
    assert _provider(_FakeTicker({})).get_classification("X") == CompanyClassification()
    assert _provider(
        _FakeTicker({"sector": 123, "industry": "  "})
    ).get_classification("X") == CompanyClassification()


def test_none_info_yields_an_empty_classification():
    # yfinance can hand back a falsy .info; the adapter treats it as "no classification".
    assert _provider(_FakeTicker(None)).get_classification("X") == CompanyClassification()


def test_info_failure_becomes_a_domain_error():
    provider = _provider(_FakeTicker(None, error=RuntimeError("429 Too Many Requests")))
    with pytest.raises(StockDataUnavailable):
        provider.get_classification("AAPL")
