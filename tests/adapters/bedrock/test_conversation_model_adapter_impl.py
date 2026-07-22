import pytest

from app.adapters.bedrock.conversation_model_adapter_impl import ConversationModelAdapterImpl
from app.domains.research.agent.entities import (
    AssistantMessage,
    ModelTurn,
    ToolCall,
    ToolOutcome,
    ToolResultsMessage,
    ToolSpec,
    UserMessage,
)
from app.domains.shared.exceptions import StockDataUnavailable


# --- Stub Bedrock client (same shape as the analysis adapters') --------------------------------


class _Block:
    def __init__(self, type, *, text=None, id=None, name=None, input=None):
        self.type = type
        self.text = text
        self.id = id
        self.name = name
        self.input = input


class _Message:
    def __init__(self, content):
        self.content = content


class _Messages:
    def __init__(self, message, recorder):
        self._message = message
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder.append(kwargs)
        return self._message


class _Client:
    def __init__(self, message):
        self.calls: list[dict] = []
        self.messages = _Messages(message, self.calls)


class _BoomMessages:
    def create(self, **kwargs):
        raise RuntimeError("bedrock exploded")


class _BoomClient:
    messages = _BoomMessages()


def _model(message) -> tuple[ConversationModelAdapterImpl, _Client]:
    client = _Client(message)
    return ConversationModelAdapterImpl(client=client), client


# --- Response parsing: content blocks -> ModelTurn ---------------------------------------------


def test_parses_text_and_tool_use_blocks_into_a_turn():
    message = _Message(
        [
            _Block("text", text="Let me screen those. "),
            _Block("tool_use", id="tu_1", name="search_stocks", input={"query": "NVDA"}),
        ]
    )
    model, _ = _model(message)
    turn = model.respond(system="sys", messages=[UserMessage("compare")], tools=[])
    assert isinstance(turn, ModelTurn)
    assert turn.text == "Let me screen those."
    assert turn.tool_calls == (ToolCall(id="tu_1", name="search_stocks", arguments={"query": "NVDA"}),)
    assert turn.wants_tools is True
    assert turn.model == ConversationModelAdapterImpl._DEFAULT_MODEL_ID


def test_a_plain_text_response_is_a_final_turn():
    model, _ = _model(_Message([_Block("text", text="The answer is 42.")]))
    turn = model.respond(system="sys", messages=[UserMessage("q")], tools=[])
    assert turn.text == "The answer is 42."
    assert turn.tool_calls == ()
    assert turn.wants_tools is False


def test_a_malformed_tool_use_block_is_skipped():
    # A tool_use block missing its id/name is dropped rather than raising.
    model, _ = _model(_Message([_Block("tool_use", name="search_stocks", input={})]))
    turn = model.respond(system="sys", messages=[UserMessage("q")], tools=[])
    assert turn.tool_calls == ()


# --- Request translation: conversation entities -> the SDK call --------------------------------


def test_translates_the_transcript_and_tools_into_the_request():
    model, client = _model(_Message([_Block("text", text="ok")]))
    tools = [ToolSpec(name="search_stocks", description="screen", input_schema={"type": "object"})]
    messages = [
        UserMessage("compare NVDA and AMD"),
        AssistantMessage("checking", (ToolCall("tu_1", "search_stocks", {"query": "NVDA"}),)),
        ToolResultsMessage((ToolOutcome("tu_1", "NVDA — $3.4T", is_error=False),)),
    ]
    model.respond(system="you are a bot", messages=messages, tools=tools)

    sent = client.calls[0]
    assert sent["system"] == "you are a bot"
    # The tool definition is passed through with its schema.
    assert sent["tools"] == [
        {"name": "search_stocks", "description": "screen", "input_schema": {"type": "object"}}
    ]
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["user", "assistant", "user"]
    # The user turn is plain text; the assistant turn carries a text + a tool_use block; the
    # tool-results turn carries a tool_result block paired to the call by id.
    assert sent["messages"][0] == {"role": "user", "content": "compare NVDA and AMD"}
    assistant_blocks = sent["messages"][1]["content"]
    assert {"type": "text", "text": "checking"} in assistant_blocks
    assert {
        "type": "tool_use",
        "id": "tu_1",
        "name": "search_stocks",
        "input": {"query": "NVDA"},
    } in assistant_blocks
    assert sent["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "NVDA — $3.4T", "is_error": False}
    ]


def test_omits_the_tools_parameter_when_none_are_offered():
    # The forced final turn passes no tools, so the request must not carry a tools key (the
    # model then has to answer in prose).
    model, client = _model(_Message([_Block("text", text="final")]))
    model.respond(system="sys", messages=[UserMessage("q")], tools=[])
    assert "tools" not in client.calls[0]


def test_vendor_failure_becomes_stock_data_unavailable():
    model = ConversationModelAdapterImpl(client=_BoomClient())
    with pytest.raises(StockDataUnavailable):
        model.respond(system="sys", messages=[UserMessage("q")], tools=[])
