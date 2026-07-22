"""Agent domain errors — framework-free; raised by use cases/wiring, mapped to HTTP by
app/endpoints/error_handlers.py (never caught in an endpoint)."""


class AgentError(Exception):
    """Base for every agent-slice error."""


class EmptyQuestion(AgentError):
    """The research question was blank after normalization."""


class AgentNotConfigured(AgentError):
    """The agent cannot be built — missing recipe row or missing model dependency."""
