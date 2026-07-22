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
from app.domains.research.agent.errors import EmptyQuestion, MissingAgentRecipe
from app.domains.research.agent.interfaces import (
    AgentRecipeRepositoryAdapter,
    ConversationModelAdapter,
    Tool,
)

logger = logging.getLogger(__name__)

# Appended to the system prompt for the forced final turn once the step budget is spent.
_FORCE_FINAL = (
    "\nYou have reached the tool-call limit. Answer now from the information already gathered; "
    "do not request any more tools."
)

_EMPTY_ANSWER_FALLBACK = (
    "I couldn't complete this research within the allowed number of steps. Please try a "
    "narrower question."
)


class RunResearchUsecase:
    def __init__(
        self,
        model: ConversationModelAdapter,
        tools: Sequence[Tool],
        recipe_repo: AgentRecipeRepositoryAdapter,
        agent_name: str,
    ) -> None:
        self._model = model
        self._tools = {tool.spec.name: tool for tool in tools}
        self._specs = tuple(tool.spec for tool in tools)
        self._recipe_repo = recipe_repo
        self._agent_name = agent_name

    def execute(self, question: str) -> ResearchResult:
        question = (question or "").strip()
        if not question:
            raise EmptyQuestion()

        # Prompt/steps come from the stored recipe per execution — a DB edit hits the next request.
        recipe = self._recipe_repo.get(self._agent_name)
        if recipe is None:
            raise MissingAgentRecipe(self._agent_name)
        system_prompt = recipe.system_prompt
        max_steps = max(1, recipe.max_steps)

        messages: list[Message] = [UserMessage(question)]
        steps: list[AgentStep] = []
        model_id = ""

        for _ in range(max_steps):
            turn = self._model.respond(
                system=system_prompt, messages=messages, tools=self._specs
            )
            model_id = turn.model or model_id
            if not turn.wants_tools:
                return ResearchResult(
                    question=question,
                    answer=turn.text.strip(),
                    model=model_id,
                    generated_at=datetime.now(timezone.utc),
                    steps=tuple(steps),
                )

            messages.append(AssistantMessage(turn.text, turn.tool_calls))
            outcomes = []
            for call in turn.tool_calls:
                step = self._run_tool(call)
                steps.append(step)
                outcomes.append(ToolOutcome(call.id, step.output, step.is_error))
            messages.append(ToolResultsMessage(tuple(outcomes)))

        # Budget spent: force one tool-free turn so the read always resolves to an answer.
        final = self._model.respond(
            system=system_prompt + _FORCE_FINAL, messages=messages, tools=()
        )
        model_id = final.model or model_id
        answer = final.text.strip() or _EMPTY_ANSWER_FALLBACK
        return ResearchResult(
            question=question,
            answer=answer,
            model=model_id,
            generated_at=datetime.now(timezone.utc),
            steps=tuple(steps),
        )

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
