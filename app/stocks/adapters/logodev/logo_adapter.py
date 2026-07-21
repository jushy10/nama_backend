import httpx

from app.stocks.company.logo.entities import Logo
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.company.logo.ports import LogoProvider


class LogoDevProvider(LogoProvider):
    _DEFAULT_BASE_URL = "https://img.logo.dev"

    def __init__(self, token: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._token = token
        self._http = httpx.Client(
            base_url=base_url,
            timeout=10.0,
            follow_redirects=True,  # the source 3xx-es to a CDN-hosted image
        )

    def get_logo(self, symbol: str) -> Logo:
        try:
            resp = self._http.get(
                f"/ticker/{symbol}",
                params={"token": self._token, "format": "png", "fallback": "404"},
            )
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code == 404:
            raise StockNotFound(symbol)
        if resp.status_code != 200:
            # Surface the upstream body so the failure is self-explaining. The
            # token rides in the request URL, not the response body, so it does
            # not leak here.
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                symbol, f"logo request failed (HTTP {resp.status_code}): {body}"
            )
        media_type = resp.headers.get("content-type", "image/png")
        return Logo(content=resp.content, media_type=media_type)
