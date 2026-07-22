"""The agent slice's domain errors.

Framework-free: raised by use cases and wiring, translated to HTTP status codes by the
central exception handlers (app/endpoints/error_handlers.py) — never caught in an
endpoint. An endpoint stays a one-liner; a new error means a new class here plus one
handler registration.
"""


class AgentError(Exception):
    """Base for every agent-slice error."""


class EmptyQuestion(AgentError):
    """The research question was blank after normalization."""


class AgentNotConfigured(AgentError):
    """The agent cannot be built — missing recipe row or missing model dependency."""
