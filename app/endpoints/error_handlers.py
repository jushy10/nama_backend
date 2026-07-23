"""Central domain-exception -> HTTP translation, registered once on the app. Fires only
for exceptions an endpoint didn't catch itself, so inline-translating slices are unaffected."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.domains.research.agent.errors import AgentNotConfigured, EmptyQuestion
from app.domains.shared.exceptions import QuotaExceeded, StockDataUnavailable, StockNotFound

_STATUS_BY_ERROR: tuple[tuple[type[Exception], int], ...] = (
    (EmptyQuestion, 400),
    (StockNotFound, 404),
    (QuotaExceeded, 429),
    (StockDataUnavailable, 502),
    (AgentNotConfigured, 503),
)


def register_error_handlers(app: FastAPI) -> None:
    for error_type, status_code in _STATUS_BY_ERROR:

        def handler(request: Request, exc: Exception, status_code=status_code) -> JSONResponse:
            return JSONResponse(status_code=status_code, content={"detail": str(exc)})

        app.add_exception_handler(error_type, handler)
