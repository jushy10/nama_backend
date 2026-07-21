"""Enterprise Business Rules: the research-agent conversation primitives.

Pure domain objects — frozen dataclasses only, no vendor SDK, no framework — that model one
turn of an agentic tool-use loop. The vendor adapter translates these to/from the Anthropic
message shape; the use case builds a conversation out of them and never sees Bedrock.

The message triad (``UserMessage`` / ``AssistantMessage`` / ``ToolResultsMessage``) is the
running transcript the use case hands back to the model each turn. ``ToolSpec`` is what a tool
advertises to the model; ``ToolCall`` is the model asking for one; ``ToolOutcome`` is the
result fed back. ``ModelTurn`` is one model response — free text plus any tool calls it wants
run. ``AgentStep`` records an executed tool call for the transcript, and ``ResearchResult`` is
the finished read.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ToolSpec:
    """What a tool advertises to the model: its name, a one-line purpose, and the JSON Schema
    for its arguments (a full ``{"type": "object", ...}`` object the adapter passes straight to
    the SDK). The model reads these to decide which tool to call and with what arguments."""

    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolCall:
    """The model asking to run one tool: the vendor's opaque call id (echoed back on the
    result so the model can match them up), the tool name, and the arguments it chose."""

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolOutcome:
    """The result of running one tool, fed back to the model on the next turn. ``content`` is
    the plain-text the model reads; ``is_error`` marks a failed call (an unknown tool, bad
    arguments, or a data source that was down) so the model can recover rather than stall."""

    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class UserMessage:
    """The human's turn — the original question, and the only free-text input to the loop."""

    text: str


@dataclass(frozen=True)
class AssistantMessage:
    """A model turn recorded back into the transcript: its narration plus the tool calls it
    made, so the next model call sees its own prior reasoning and requests."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolResultsMessage:
    """The results of the tool calls from the preceding assistant turn, handed back as one
    message (the vendor pairs each outcome to its call by id)."""

    outcomes: tuple[ToolOutcome, ...]


# One entry in the running transcript. A union rather than a base class so each shape carries
# only the fields it needs and the adapter can match on type.
Message = UserMessage | AssistantMessage | ToolResultsMessage


@dataclass(frozen=True)
class ModelTurn:
    """One model response. ``text`` is its narration; ``tool_calls`` is any capabilities it
    wants run before continuing. An empty ``tool_calls`` means the model is done and ``text``
    is the final answer. ``model`` is the id that produced it, carried through to the result."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        """True when the model asked to run at least one tool — the loop must continue."""
        return bool(self.tool_calls)


@dataclass(frozen=True)
class AgentStep:
    """One executed tool call, kept for the transcript the endpoint surfaces: which tool ran,
    the arguments the model chose, the text it got back, and whether that call failed. This is
    the observability seam — it makes the agent's reasoning path inspectable end to end."""

    tool: str
    arguments: dict
    output: str
    is_error: bool = False


@dataclass(frozen=True)
class ResearchResult:
    """The finished research read: the question, the model's final answer, the ordered steps it
    took to get there, the model id, and when it ran. ``steps`` is empty when the model answered
    without calling any tool."""

    question: str
    answer: str
    model: str
    generated_at: datetime
    steps: tuple[AgentStep, ...] = field(default_factory=tuple)
