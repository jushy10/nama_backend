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
from app.stocks.fmp_profile_provider import FmpProfileProvider, _summarize


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


# --------------------------- _summarize (condensing) ---------------------------

def test_summarize_keeps_first_two_sentences():
    text = "First sentence here. Second one follows. Third should be dropped."
    assert _summarize(text) == "First sentence here. Second one follows."


def test_summarize_does_not_split_on_company_suffix():
    # "Inc." must not be read as a sentence end (it's followed by lowercase, and
    # is also guarded) — otherwise the blurb would be just "Apple Inc.".
    text = "Apple Inc. is a tech company. It makes phones and computers. More text."
    assert _summarize(text) == "Apple Inc. is a tech company. It makes phones and computers."


def test_summarize_guards_abbreviation_before_capital():
    # "Co. The" looks like a boundary (capital follows) but "Co." is an
    # abbreviation, so the two visual sentences stay merged as one.
    text = "Bought by Globex Co. The deal closed. Apple makes phones. Extra one."
    assert _summarize(text) == "Bought by Globex Co. The deal closed. Apple makes phones."


def test_summarize_passes_through_short_text():
    assert _summarize("Just one sentence.") == "Just one sentence."


def test_summarize_collapses_whitespace():
    assert _summarize("A  line.\n\nNext  line.") == "A line. Next line."


def test_summarize_caps_runaway_sentence_with_ellipsis():
    text = "word " * 200  # no sentence break, far past the char cap
    out = _summarize(text)
    assert out.endswith("…")
    assert len(out) <= 301  # _MAX_CHARS + the ellipsis


def test_get_profile_condenses_long_description():
    long = "Apple Inc. is a global tech company. It designs devices. " + (
        "Filler sentence. " * 20
    )
    p = provider_with(FakeResponse(json_data=[{"description": long}]))
    assert p.get_profile("AAPL").description == (
        "Apple Inc. is a global tech company. It designs devices."
    )
