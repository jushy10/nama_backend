"""The app-wide per-client (per-IP) rate limiter wired in ``app/main.py``.

The autouse fixture in ``conftest`` disables the limiter for the rest of the
suite (every ``TestClient`` request otherwise shares one client key and would
pool the whole run into a single bucket). These tests re-enable it in-body and
drive it through the real middleware, giving each client a distinct IP via
``X-Forwarded-For`` — the header the key function reads and the gateway
overwrites with the true source in production.
"""

from starlette.testclient import TestClient

from app.main import app, limiter


def _hammer(client: TestClient, ip: str, n: int) -> list[int]:
    headers = {"X-Forwarded-For": ip}
    return [client.get("/healthz", headers=headers).status_code for _ in range(n)]


def test_bursting_past_the_per_ip_limit_gets_throttled():
    limiter.enabled = True
    client = TestClient(app)

    # 50 is comfortably past twice the 20/second allowance, so some requests are
    # throttled wherever the burst falls relative to the window boundary.
    codes = _hammer(client, "203.0.113.10", 50)

    assert 200 in codes  # the client isn't blocked outright...
    assert 429 in codes  # ...but its burst is throttled
    assert codes.count(200) <= 40  # ~20/second (at most two adjacent windows)


def test_each_client_ip_gets_its_own_allowance():
    # The whole point of keying on the client IP: one abusive caller burning its
    # allowance must not throttle a different client.
    limiter.enabled = True
    client = TestClient(app)

    abuser = _hammer(client, "203.0.113.20", 50)
    bystander = _hammer(client, "203.0.113.21", 5)

    assert 429 in abuser  # one IP exhausts its own bucket...
    assert bystander == [200] * 5  # ...the next IP is unaffected
