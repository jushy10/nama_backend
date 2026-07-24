from dataclasses import dataclass
from datetime import date, datetime

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


class ToolResult:
    """Marker base for the model-facing tool payloads. What a tool run returns is
    serialized verbatim as the tool_result the model reads — so each payload class
    is the deliberate selection of what the model gets to see."""


@dataclass(frozen=True)
class ToolMessage(ToolResult):
    """A structured non-answer — 'nothing matched', 'source unavailable' — so the
    model can tell an empty result from a broken tool."""

    message: str


@dataclass(frozen=True)
class ToolError(ToolResult):
    """A failed tool call, reported to the model as data (not prose) so it can adjust:
    a stable ``error`` code, plus what it asked for and what it could ask for instead."""

    error: str  # "unknown_tool" | "tool_failed"
    tool: str
    detail: str | None = None
    available_tools: tuple[str, ...] | None = None


@dataclass(frozen=True)
class VixReading:
    value: float
    change: float | None
    regime: str
    as_of: date


@dataclass(frozen=True)
class FearGreedReading:
    score: float
    label: str
    cnn_rating: str


@dataclass(frozen=True)
class MarketSentimentResult(ToolResult):
    vix: VixReading | None
    fear_greed: FearGreedReading | None


@dataclass(frozen=True)
class StockScreenRow:
    ticker: str
    name: str | None
    sector: str | None
    market_cap: float | None
    pe_ratio: float | None
    revenue_growth_yoy: float | None


@dataclass(frozen=True)
class StockScreenResult(ToolResult):
    total: int
    results: tuple[StockScreenRow, ...]


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
