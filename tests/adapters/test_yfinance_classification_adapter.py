import pytest

from app.stocks.adapters.yfinance_classification_adapter import (
    YfinanceClassificationProvider,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import CompanyClassification


class _FakeTicker:
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


def test_captures_the_issuer_domicile_as_iso2():
    # info['country'] is Yahoo's display name; the entity maps it to an ISO-2 domicile.
    provider = _provider(
        _FakeTicker(
            {
                "sector": "Technology",
                "industry": "Semiconductors",
                "country": "Taiwan",
            }
        )
    )
    assert provider.get_classification("TSM") == CompanyClassification(
        sector="technology", industry="semiconductors", domicile_country="TW"
    )


def test_unrecognized_or_missing_country_yields_no_domicile():
    # A country Yahoo names but the ISO-2 map doesn't cover, and an absent country, both leave
    # the domicile None (an unknown domicile the search shows in its listing market).
    assert _provider(
        _FakeTicker({"sector": "Technology", "country": "Atlantis"})
    ).get_classification("X") == CompanyClassification(sector="technology")
    assert _provider(
        _FakeTicker({"industry": "Software"})
    ).get_classification("X") == CompanyClassification(industry="software")


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
