import logging
from collections.abc import Sequence

from app.adapters.bedrock.cost import log_model_cost
from app.domains.research.agent.entities import (
    AssistantMessage,
    Message,
    ModelTurn,
    ToolCall,
    ToolSpec,
    UserMessage,
)
from app.domains.research.agent.interfaces import ConversationModelAdapter
from app.domains.shared.exceptions import StockDataUnavailable

logger = logging.getLogger(__name__)


class ConversationModelAdapterImpl(ConversationModelAdapter):
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # A ceiling, not spend — sized so a broad final answer never clips (see respond()).
    _MAX_TOKENS = 2048

    def __init__(
        self,
        *,
        model_id: str = _DEFAULT_MODEL_ID,
        region: str = _DEFAULT_REGION,
        max_tokens: int = _MAX_TOKENS,
        client=None,
    ) -> None:
        self._model_id = model_id
        self._max_tokens = max_tokens
        if client is not None:
            self._client = client
            return
        # Lazy import: boto3 is heavy and optional; ImportError -> 503 in the wiring.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def respond(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> ModelTurn:
        request: dict = {
            "model": self._model_id,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [_to_anthropic_message(m) for m in messages],
        }
        # The forced final turn passes no tools — omit the param so the model must answer in prose.
        if tools:
            request["tools"] = [_to_anthropic_tool(spec) for spec in tools]
        try:
            message = self._client.messages.create(**request)
        except Exception as exc:  # SDK/botocore raise a family of errors; map them all
            raise StockDataUnavailable(
                "research", f"research model call failed: {exc}"
            ) from exc
        log_model_cost(label="ai research", model_id=self._model_id, message=message)
        if getattr(message, "stop_reason", None) == "max_tokens":
            # A clipped tool_use block silently reads as a final answer — surface it loudly.
            logger.warning(
                "research model turn truncated at max_tokens=%s (model %s)",
                self._max_tokens,
                self._model_id,
            )
        return _to_turn(message, self._model_id)


def _to_anthropic_message(message: Message) -> dict:
    if isinstance(message, UserMessage):
        return {"role": "user", "content": message.text}
    if isinstance(message, AssistantMessage):
        content: list[dict] = []
        if message.text:
            content.append({"type": "text", "text": message.text})
        for call in message.tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
            )
        return {"role": "assistant", "content": content}
    # ToolResultsMessage: tool_result blocks ride a user turn, paired by tool_use_id.
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": outcome.call_id,
                "content": outcome.content,
                "is_error": outcome.is_error,
            }
            for outcome in message.outcomes
        ],
    }


def _to_anthropic_tool(spec: ToolSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
    }


def _to_turn(message, model_id: str) -> ModelTurn:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in getattr(message, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text" and isinstance(getattr(block, "text", None), str):
            texts.append(block.text)
        elif kind == "tool_use" and isinstance(getattr(block, "id", None), str):
            arguments = getattr(block, "input", None)
            calls.append(
                ToolCall(
                    id=block.id,
                    name=str(getattr(block, "name", "")),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
    return ModelTurn(
        text="".join(texts).strip(),
        tool_calls=tuple(calls),
        model=model_id,
    )
