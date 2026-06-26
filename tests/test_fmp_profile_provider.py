"""Unit tests for the FMP company-profile adapter.

No network: the httpx client is swapped for a fake that returns a queued
response per GET (so a test can script the stable call failing and the legacy
call answering). Verifies the adapter's jobs — pull ``description`` out of FMP's
profile list, fall back stable -> legacy, and translate failures into domain
errors.
"""

import httpx
import pytest

from app.stocks.entities import CompanyProfile
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.fmp_profile_provider import FmpProfileProvider


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", json_error=None):
        self.status_code = status_code
        self._json = [] if json_data is None else json_data
        self.text = text
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._json


class FakeHttpClient:
    """Returns queued items one per GET: a FakeResponse is returned, an Exception
    is raised — so a test can script stable failing then legacy succeeding."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.requests.append((url, params or {}))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def provider_with(*responses) -> FmpProfileProvider:
    p = FmpProfileProvider("dummy-key")
    p._http = FakeHttpClient(responses)
    return p


def test_maps_description_from_stable():
    p = provider_with(FakeResponse(json_data=[{"description": "Apple designs phones."}]))
    profile = p.get_profile("AAPL")
    assert isinstance(profile, CompanyProfile)
    assert profile.description == "Apple designs phones."


def test_maps_clean_company_name_from_stable():
    # FMP's companyName is the tidy display name; it backs the stock view's name.
    p = provider_with(
        FakeResponse(json_data=[{"companyName": "Apple Inc.", "description": "x"}])
    )
    profile = p.get_profile("AAPL")
    assert profile.name == "Apple Inc."
    assert profile.description == "x"


def test_blank_company_name_normalized_to_none():
    p = provider_with(FakeResponse(json_data=[{"companyName": "   ", "description": "x"}]))
    assert p.get_profile("AAPL").name is None


def test_missing_company_name_yields_none():
    p = provider_with(FakeResponse(json_data=[{"description": "x"}]))
    assert p.get_profile("AAPL").name is None


def test_empty_list_yields_none_name():
    p = provider_with(FakeResponse(json_data=[]))
    assert p.get_profile("ZZZZ").name is None


def test_sends_symbol_and_apikey_to_stable_first():
    p = provider_with(FakeResponse(json_data=[{"description": "x"}]))
    p.get_profile("AAPL")
    url, params = p._http.requests[0]
    assert url == "/stable/profile"
    assert params == {"symbol": "AAPL", "apikey": "dummy-key"}


def test_stable_success_does_not_call_legacy():
    p = provider_with(FakeResponse(json_data=[{"description": "x"}]))
    p.get_profile("AAPL")
    assert len(p._http.requests) == 1  # legacy untouched when stable answers


def test_falls_back_to_legacy_on_non_200():
    p = provider_with(
        FakeResponse(status_code=403, text="legacy-only key"),
        FakeResponse(json_data=[{"description": "From legacy."}]),
    )
    profile = p.get_profile("AAPL")
    assert profile.description == "From legacy."
    assert p._http.requests[1][0] == "/api/v3/profile/AAPL"
    assert p._http.requests[1][1] == {"apikey": "dummy-key"}


def test_falls_back_to_legacy_on_transport_error():
    p = provider_with(
        httpx.ConnectError("boom"),
        FakeResponse(json_data=[{"description": "Recovered."}]),
    )
    assert p.get_profile("AAPL").description == "Recovered."


def test_empty_list_yields_none_description():
    # Unknown symbol -> FMP returns [] with HTTP 200; best-effort -> no description.
    p = provider_with(FakeResponse(json_data=[]))
    assert p.get_profile("ZZZZ").description is None


def test_blank_description_normalized_to_none():
    p = provider_with(FakeResponse(json_data=[{"description": "   "}]))
    assert p.get_profile("AAPL").description is None


def test_missing_description_key_yields_none():
    p = provider_with(FakeResponse(json_data=[{"companyName": "Apple Inc."}]))
    assert p.get_profile("AAPL").description is None


def test_both_endpoints_non_200_raises_unavailable_with_last_body():
    p = provider_with(
        FakeResponse(status_code=429, text="rate limited"),
        FakeResponse(status_code=500, text="upstream boom"),
    )
    with pytest.raises(StockDataUnavailable) as exc:
        p.get_profile("AAPL")
    assert "500" in str(exc.value)
    assert "upstream boom" in str(exc.value)  # last error surfaced for debugging
    assert len(p._http.requests) == 2


def test_transport_error_on_both_raises_unavailable():
    p = provider_with(httpx.ConnectError("a"), httpx.ConnectError("b"))
    with pytest.raises(StockDataUnavailable):
        p.get_profile("AAPL")


def test_invalid_json_on_both_raises_unavailable():
    p = provider_with(
        FakeResponse(json_error=ValueError("not json")),
        FakeResponse(json_error=ValueError("still not json")),
    )
    with pytest.raises(StockDataUnavailable):
        p.get_profile("AAPL")


def test_full_description_passes_through_unchanged():
    # The adapter returns FMP's description verbatim — no condensing/truncation.
    text = (
        "Apple Inc. is a global tech company. It designs devices. "
        "It also runs services and a retail network."
    )
    p = provider_with(FakeResponse(json_data=[{"description": text}]))
    assert p.get_profile("AAPL").description == text
