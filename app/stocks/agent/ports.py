"""Application ports: the two abstractions the research-agent use case depends on.

``ConversationModel`` is one turn of the model — given the system prompt, the running
transcript, and the tools on offer, return what the model says and which tools it wants run.
The loop (how many turns, running the tools, feeding results back) is the use case's job, not
the model's, so this port is deliberately stateless and single-turn: swap Bedrock for another
provider and only its adapter changes.

``Tool`` is one capability the agent can call. Each concrete tool delegates to another slice's
read use case; it advertises a ``ToolSpec`` to the model and runs its arguments into a
plain-text result. Tools translate their own failures into an error string rather than raising,
so one bad call degrades to a message the model can react to instead of sinking the whole read.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.stocks.agent.entities import Message, ModelTurn, ToolSpec


class ConversationModel(ABC):
    """A gateway for one turn of an agentic conversation with a tool-using model."""

    @abstractmethod
    def respond(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> ModelTurn:
        """Run one model turn over the transcript and return its response.

        The model may answer directly (``ModelTurn`` with no tool calls) or ask to run one or
        more of ``tools`` first (tool calls set). ``tools`` may be empty to force a final,
        tool-free answer (the use case does this once the step budget is spent).

        Raises:
            StockDataUnavailable: the model call failed (mapped to a 502 at the edge).
        """
        raise NotImplementedError


class Tool(ABC):
    """One capability the agent can call, backed by a slice's read use case."""

    @property
    @abstractmethod
    def spec(self) -> ToolSpec:
        """What this tool advertises to the model: name, purpose, and argument schema."""
        raise NotImplementedError

    @abstractmethod
    def run(self, arguments: dict) -> str:
        """Execute the tool against ``arguments`` and return a plain-text result the model can
        read. A tool reports a data problem by returning an explanatory string (the use case
        marks the outcome an error); it should not raise for an ordinary bad-input / no-data
        case, so the model can recover within the loop."""
        raise NotImplementedError
