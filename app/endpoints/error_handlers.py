"""Central domain-exception -> HTTP translation.

Registered once on the app so endpoints stay one-liners: a use case raises a domain error
and the matching handler here turns it into the right status code. Handlers only fire for
exceptions an endpoint did not catch itself, so slices that still translate inline are
unaffected.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.domains.research.agent.errors import AgentNotConfigured, EmptyQuestion
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound

_STATUS_BY_ERROR: tuple[tuple[type[Exception], int], ...] = (
    (EmptyQuestion, 400),
    (StockNotFound, 404),
    (StockDataUnavailable, 502),
    (AgentNotConfigured, 503),
)


def register_error_handlers(app: FastAPI) -> None:
    for error_type, status_code in _STATUS_BY_ERROR:

        def handler(request: Request, exc: Exception, status_code=status_code) -> JSONResponse:
            return JSONResponse(status_code=status_code, content={"detail": str(exc)})

        app.add_exception_handler(error_type, handler)
