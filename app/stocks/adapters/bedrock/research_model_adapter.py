from collections.abc import Sequence

from app.stocks.adapters.bedrock.cost import log_model_cost
from app.stocks.ai.agent.entities import (
    AssistantMessage,
    Message,
    ModelTurn,
    ToolCall,
    ToolSpec,
    UserMessage,
)
from app.stocks.ai.agent.ports import ConversationModel
from app.stocks.exceptions import StockDataUnavailable


class BedrockConversationModel(ConversationModel):
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # One turn is a short narration plus a tool call or two, or the final answer — a modest cap
    # is ample and keeps a runaway turn from ballooning the token bill.
    _MAX_TOKENS = 1024

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
        # Imported here, not at module load: the SDK is an optional heavyweight dependency (it
        # pulls boto3) that neither the app's other endpoints nor the offline tests need. A
        # missing extra raises ImportError, which the wiring turns into a 503.
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
        # Offer tools only when the use case supplies them; on the forced final turn it passes
        # none, so the model must answer in prose (no tools param, no tool_choice).
        if tools:
            request["tools"] = [_to_anthropic_tool(spec) for spec in tools]
        try:
            message = self._client.messages.create(**request)
        except Exception as exc:  # SDK/botocore raise a family of errors; map them all
            raise StockDataUnavailable(
                "research", f"research model call failed: {exc}"
            ) from exc
        log_model_cost(label="ai research", model_id=self._model_id, message=message)
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
    # ToolResultsMessage — the tool outputs, paired to their calls by id, sent as a user turn.
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
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in getattr(message, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            piece = getattr(block, "text", None)
            if isinstance(piece, str):
                text_parts.append(piece)
        elif kind == "tool_use":
            name = getattr(block, "name", None)
            call_id = getattr(block, "id", None)
            arguments = getattr(block, "input", None)
            if isinstance(name, str) and isinstance(call_id, str):
                tool_calls.append(
                    ToolCall(
                        id=call_id,
                        name=name,
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )
    return ModelTurn(
        text="".join(text_parts).strip(),
        tool_calls=tuple(tool_calls),
        model=model_id,
    )
