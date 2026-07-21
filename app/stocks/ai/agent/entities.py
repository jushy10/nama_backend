from dataclasses import dataclass
from datetime import datetime


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


# One entry in the running transcript. A union rather than a base class so each shape carries
# only the fields it needs and the adapter can match on type.
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
