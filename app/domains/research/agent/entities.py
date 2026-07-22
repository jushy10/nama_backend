from dataclasses import dataclass
from datetime import datetime

# Every research answer ships this disclaimer — authored by the service, never the model.
RESEARCH_DISCLAIMER = (
    "AI-generated for informational and educational purposes only — not financial advice. "
    "Markets carry risk; do your own research before investing."
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolResultsMessage:
    outcomes: tuple[ToolOutcome, ...]


# One transcript entry — a union (not a base class) so the adapter can match on type.
Message = UserMessage | AssistantMessage | ToolResultsMessage


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True)
class AgentRecipe:
    """An agent's stored configuration (the ``agent_recipes`` table, changed via migration).
    ``tool_names`` reference the code-side registry — the DB stores *which* tools, the code
    stores *how*."""

    name: str
    system_prompt: str
    tool_names: tuple[str, ...]
    max_steps: int
    model_id: str


@dataclass(frozen=True)
class AgentStep:
    tool: str
    arguments: dict
    output: str
    is_error: bool = False


@dataclass(frozen=True)
class ResearchResult:
    question: str
    answer: str
    model: str
    generated_at: datetime
    steps: tuple[AgentStep, ...] = ()
