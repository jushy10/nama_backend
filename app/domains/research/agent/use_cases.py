import logging
from collections.abc import Sequence
from datetime import datetime, timezone

from app.domains.research.agent.entities import (
    AgentStep,
    AssistantMessage,
    Message,
    ResearchResult,
    ToolCall,
    ToolOutcome,
    ToolResultsMessage,
    UserMessage,
)
from app.domains.research.agent.errors import EmptyQuestion
from app.domains.research.agent.interfaces import ConversationModelAdapter, Tool

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a stock-research assistant for a US/Canada equity screener. Answer the user's "
    "question using ONLY the tools provided — do not rely on memorized figures, which may be "
    "stale, and never invent a ticker, price, or statistic. Call a tool to get real data, read "
    "its result, and call more tools if you need to before answering.\n"
    "Rules:\n"
    "- Ground every specific number or ticker you state in a tool result from this "
    "conversation. If the tools can't answer, say so plainly rather than guessing.\n"
    "- Be concise and neutral. Explain what the data shows; do not tell the user what to buy, "
    "sell, or hold, and do not give personalized investment advice. If asked for a personal "
    "recommendation (e.g. 'should I put my savings in X'), explain the trade-offs the data "
    "shows and decline to advise.\n"
    "- When you have enough to answer, respond in plain text with no further tool calls."
)

# Each step is a metered model call — caps the spend of a runaway or looping model.
_DEFAULT_MAX_STEPS = 6

# Appended to the system prompt for the forced final turn once the step budget is spent.
_FORCE_FINAL = (
    "\nYou have reached the tool-call limit. Answer now from the information already gathered; "
    "do not request any more tools."
)

_EMPTY_ANSWER_FALLBACK = (
    "I couldn't complete this research within the allowed number of steps. Please try a "
    "narrower question."
)


class RunResearch:
    def __init__(
        self,
        model: ConversationModelAdapter,
        tools: Sequence[Tool],
        *,
        max_steps: int = _DEFAULT_MAX_STEPS,
        system_prompt: str = _SYSTEM_PROMPT,
    ) -> None:
        self._model = model
        self._tools = {tool.spec.name: tool for tool in tools}
        self._specs = tuple(tool.spec for tool in tools)
        self._max_steps = max(1, max_steps)
        self._system_prompt = system_prompt

    def execute(self, question: str) -> ResearchResult:
        question = (question or "").strip()
        if not question:
            raise EmptyQuestion("A research question must not be empty.")

        messages: list[Message] = [UserMessage(question)]
        steps: list[AgentStep] = []
        model_id = ""

        for _ in range(self._max_steps):
            turn = self._model.respond(
                system=self._system_prompt, messages=messages, tools=self._specs
            )
            model_id = turn.model or model_id
            if not turn.wants_tools:
                return self._result(question, turn.text, steps, model_id)

            messages.append(AssistantMessage(turn.text, turn.tool_calls))
            outcomes = []
            for call in turn.tool_calls:
                step = self._run_tool(call)
                steps.append(step)
                outcomes.append(ToolOutcome(call.id, step.output, step.is_error))
            messages.append(ToolResultsMessage(tuple(outcomes)))

        # Budget spent: force one tool-free turn so the read always resolves to an answer.
        final = self._model.respond(
            system=self._system_prompt + _FORCE_FINAL, messages=messages, tools=()
        )
        model_id = final.model or model_id
        answer = final.text.strip() or _EMPTY_ANSWER_FALLBACK
        return self._result(question, answer, steps, model_id)

    def _run_tool(self, call: ToolCall) -> AgentStep:
        tool = self._tools.get(call.name)
        if tool is None:
            known = ", ".join(self._tools)
            message = f"Unknown tool '{call.name}'. Available tools: {known}."
            return AgentStep(call.name, call.arguments, message, is_error=True)
        try:
            return AgentStep(call.name, call.arguments, tool.run(call.arguments))
        except Exception as exc:  # a tool should not raise, but never let one stall the loop
            logger.warning("research tool %s raised: %s", call.name, exc)
            message = f"Tool '{call.name}' failed: {exc}"
            return AgentStep(call.name, call.arguments, message, is_error=True)

    @staticmethod
    def _result(
        question: str, answer: str, steps: list[AgentStep], model_id: str
    ) -> ResearchResult:
        return ResearchResult(
            question=question,
            answer=answer.strip(),
            model=model_id,
            generated_at=datetime.now(timezone.utc),
            steps=tuple(steps),
        )
