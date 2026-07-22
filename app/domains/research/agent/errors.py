"""Agent domain errors — framework-free; raised by use cases/wiring, mapped to HTTP by
app/endpoints/error_handlers.py (never caught in an endpoint).

Each error carries its own message, so a raise site never passes one in."""


class AgentError(Exception):
    """Base for every agent-slice error."""


class EmptyQuestion(AgentError):
    def __init__(self) -> None:
        super().__init__("A research question must not be empty.")


class AgentNotConfigured(AgentError):
    """Base for the misconfiguration errors (mapped to 503)."""


class MissingAgentRecipe(AgentNotConfigured):
    def __init__(self, agent_name: str) -> None:
        super().__init__(f"No stored recipe for agent '{agent_name}' — run migrations.")


class BedrockNotInstalled(AgentNotConfigured):
    def __init__(self) -> None:
        super().__init__("AI research is not configured (install the 'bedrock' extra).")
