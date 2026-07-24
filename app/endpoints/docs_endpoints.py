"""The split API docs: a public Swagger page for the read API and a staff-only
page for the ``/internal/*`` cron surface.

FastAPI's built-in docs are disabled in ``app/main.py``; these routes serve two
filtered replacements off the same live route table, split by the ``/internal/``
path prefix — so the split can never drift from the actual routing. The internal
pair is guarded by HTTP Basic (``INTERNAL_DOCS_USERNAME`` /
``INTERNAL_DOCS_PASSWORD``) rather than the cron bearer token, because Swagger UI
fetches ``openapi.json`` with a plain browser request that carries no
``Authorization`` header — after the browser's Basic prompt on ``/internal/docs``
it re-attaches the credentials to that fetch automatically, which a bearer scheme
can't do.
"""

import os
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import iter_route_contexts
from fastapi.security import HTTPBasic, HTTPBasicCredentials

router = APIRouter()

# ``auto_error=False`` so a missing/non-Basic Authorization header yields ``None`` here
# instead of HTTPBasic raising its own error — we want one uniform 401 (with a
# ``WWW-Authenticate: Basic`` challenge, which is what makes the browser prompt) for
# every "no valid credentials" case, and the 503 "not configured" check to take
# precedence over it. Same shape as the cron token guard (app/endpoints/cron/auth.py).
_basic = HTTPBasic(
    auto_error=False,
    description=(
        "Staff docs credentials: INTERNAL_DOCS_USERNAME / INTERNAL_DOCS_PASSWORD."
    ),
)


def require_docs_auth(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> None:
    expected_username = os.environ.get("INTERNAL_DOCS_USERNAME")
    expected_password = os.environ.get("INTERNAL_DOCS_PASSWORD")
    if not expected_username or not expected_password:
        # Fail-closed while unconfigured, like the cron token guard.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Internal docs are not configured "
            "(INTERNAL_DOCS_USERNAME / INTERNAL_DOCS_PASSWORD).",
        )
    provided_username = credentials.username if credentials else ""
    provided_password = credentials.password if credentials else ""
    # Compare both legs unconditionally (no short-circuit leaking which one failed)
    # and as bytes: secrets.compare_digest rejects str with non-ASCII, and encoding
    # both sides sidesteps that entirely while staying constant-time.
    username_ok = secrets.compare_digest(
        provided_username.encode("utf-8"), expected_username.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        provided_password.encode("utf-8"), expected_password.encode("utf-8")
    )
    if not (username_ok and password_ok):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or missing internal docs credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )


def _is_internal(route: Any) -> bool:
    return (getattr(route, "path", "") or "").startswith("/internal/")


def _openapi_schema(request: Request, *, internal: bool) -> dict[str, Any]:
    # The route table is fixed after startup, so each variant is generated once per
    # process and cached on app.state (mirroring FastAPI's own openapi_schema cache).
    app = request.app
    cache = getattr(app.state, "split_openapi_cache", None)
    if cache is None:
        cache = app.state.split_openapi_cache = {}
    key = "internal" if internal else "public"
    if key not in cache:
        audience = "internal (staff)" if internal else "public API"
        # ``app.routes`` holds lazy ``_IncludedRouter`` wrappers (whose ``path`` is
        # unset), so flatten to effective per-route contexts first — the same helper
        # ``get_openapi`` itself uses — and filter on each context's real path.
        route_contexts = [
            context
            for context in iter_route_contexts(app.routes)
            if _is_internal(context) == internal
        ]
        cache[key] = get_openapi(
            title=f"{app.title} — {audience}",
            version=app.version,
            routes=route_contexts,
        )
    return cache[key]


def _swagger_page(request: Request, *, openapi_path: str, title: str) -> HTMLResponse:
    # Prefix with the ASGI root_path (as FastAPI's built-in docs do) so the page
    # still finds its schema when the app is served under a gateway stage prefix.
    root_path = request.scope.get("root_path", "").rstrip("/")
    return get_swagger_ui_html(openapi_url=f"{root_path}{openapi_path}", title=title)


@router.get("/openapi.json", include_in_schema=False)
def public_openapi(request: Request) -> JSONResponse:
    return JSONResponse(_openapi_schema(request, internal=False))


@router.get("/docs", include_in_schema=False)
def public_docs(request: Request) -> HTMLResponse:
    return _swagger_page(
        request, openapi_path="/openapi.json", title=f"{request.app.title} — docs"
    )


@router.get("/internal/openapi.json", include_in_schema=False)
def internal_openapi(
    request: Request, _: None = Depends(require_docs_auth)
) -> JSONResponse:
    return JSONResponse(_openapi_schema(request, internal=True))


@router.get("/internal/docs", include_in_schema=False)
def internal_docs(
    request: Request, _: None = Depends(require_docs_auth)
) -> HTMLResponse:
    return _swagger_page(
        request,
        openapi_path="/internal/openapi.json",
        title=f"{request.app.title} — internal docs",
    )
