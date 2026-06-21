"""Unit tests for the FMP logo adapter.

No network: the httpx client is swapped for a fake. Verifies the adapter's two
jobs — return a Logo on success, and translate HTTP failures into domain errors.
"""

from types import SimpleNamespace

import httpx
import pytest

from app.stocks.entities import Logo
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.fmp_logo_provider import FmpLogoProvider


class FakeHttpClient:
    def __init__(self, status_code=200, content=b"", text="", headers=None, error=None):
        self._status_code = status_code
        self._content = content
        self._text = text
        self._headers = headers or {}
        self._error = error
        self.requested: list[str] = []

    def get(self, url):
        self.requested.append(url)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            status_code=self._status_code,
            content=self._content,
            text=self._text,
            headers=self._headers,
        )


def provider_with(http_client) -> FmpLogoProvider:
    # Construction is offline (the httpx client makes no call until used); then
    # swap in the fake so get_logo() makes no network calls.
    p = FmpLogoProvider()
    p._http = http_client
    return p


def test_returns_logo_with_upstream_media_type():
    http = FakeHttpClient(
        status_code=200, content=b"\x89PNG\r\n", headers={"content-type": "image/png"}
    )
    p = provider_with(http)
    logo = p.get_logo("AAPL")
    assert isinstance(logo, Logo)
    assert logo.content == b"\x89PNG\r\n"
    assert logo.media_type == "image/png"
    assert http.requested == ["/AAPL.png"]


def test_media_type_defaults_to_png_when_absent():
    p = provider_with(FakeHttpClient(status_code=200, content=b"x", headers={}))
    assert p.get_logo("AAPL").media_type == "image/png"


def test_404_raises_not_found():
    p = provider_with(FakeHttpClient(status_code=404))
    with pytest.raises(StockNotFound):
        p.get_logo("ZZZZ")


def test_other_status_raises_unavailable_with_body():
    p = provider_with(FakeHttpClient(status_code=403, text="forbidden"))
    with pytest.raises(StockDataUnavailable) as exc:
        p.get_logo("AAPL")
    assert "403" in str(exc.value)
    assert "forbidden" in str(exc.value)  # upstream body surfaced for debugging


def test_transport_error_raises_unavailable():
    p = provider_with(FakeHttpClient(error=httpx.ConnectError("boom")))
    with pytest.raises(StockDataUnavailable):
        p.get_logo("AAPL")


def test_client_follows_redirects():
    # The source may 3xx to a CDN; httpx won't follow unless told to.
    assert FmpLogoProvider()._http.follow_redirects is True
