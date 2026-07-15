"""The app-wide per-client (per-IP) rate limiter.

Lives in its own module rather than ``app/main.py`` so endpoint modules can
attach per-route limits with ``@limiter.limit(...)`` without importing ``main``.
``main`` imports every endpoint router at module top, so the limiter has to sit
*upstream* of both — importing it here keeps the dependency one-directional and
avoids a cycle. ``main`` installs it (state + exception handler + middleware);
the endpoint modules only decorate their routes with it.
"""

import os

from slowapi import Limiter
from starlette.requests import Request


def _client_ip(request: Request) -> str:
    """Identify the caller for rate limiting.

    Behind the API Gateway VPC link the socket peer is the gateway's ENI — the
    same address for every caller — so keying on ``request.client.host`` would
    lump all traffic into one bucket. The real client IP arrives in the
    ``X-Client-IP`` header, which the gateway *overwrites* with the observed
    source IP (see the integration's request_parameters in infra), so it's
    trustworthy and can't be spoofed by a client-supplied header. (It's a custom
    header rather than X-Forwarded-For because API Gateway v2 forbids mapping
    operations on XFF.)

    The X-Forwarded-For fallback covers running without the gateway in front
    (local dev, tests); off the gateway there's nothing to overwrite the header,
    so treat it as untrusted best-effort keying, not a security boundary.
    """
    stamped = request.headers.get("x-client-ip")
    if stamped:
        return stamped.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "anonymous"


# The counter defaults to in-process ("memory://"), which is exact for a single
# task. Under autoscaling the service can run several tasks, and an in-process
# counter is then per-task — a single IP can reach up to (task count) * the limit,
# with the API Gateway throttle as the hard global backstop. Set
# RATE_LIMIT_STORAGE_URI to a shared store (e.g. redis://host:6379) to make the
# count exact across tasks; it's a one-env-var flip, no code change.
_rate_limit_storage = os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://")

# Per-client (per-IP) rate limiting so one abusive caller can't exhaust the
# service — a token bucket per client IP; over it, SlowAPI raises
# RateLimitExceeded and the handler (installed in app/main.py) returns HTTP 429.
# These limits sit under API Gateway's global throttle: that caps total
# load/cost, this stops any single IP from consuming it. The ``default_limits``
# apply to every route; expensive routes layer a tighter ``@limiter.limit(...)``
# on top (their own bucket, checked in addition to these). Tune as traffic grows.
limiter = Limiter(
    key_func=_client_ip,
    default_limits=["20/second", "600/minute"],
    storage_uri=_rate_limit_storage,
)
