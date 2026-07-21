"""Tests for the research-agent loop (RunResearch).

Offline and vendor-free: a scripted fake ``ConversationModel`` returns a canned sequence of
model turns (recording what it was asked each call), and fake tools return canned output or
raise. This exercises the whole loop policy — direct answers, single- and multi-tool rounds,
the transcript fed back to the model, unknown-tool and tool-error recovery, and the forced final
turn when the step budget is spent — with no Bedrock and no network.
"""

import pytest

from app.stocks.agent.entities import ModelTurn, ToolCall, ToolSpec
from app.stocks.agent.ports import Tool
from app.stocks.agent.use_cases import (
    _EMPTY_ANSWER_FALLBACK,
    RunResearch,
)


class _ScriptedModel:
    """Returns the next scripted ModelTurn each call, recording the (system, messages, tools)
    it was handed so the test can assert on the transcript and the tool offering."""

    def __init__(self, turns) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []

    def respond(self, *, system, messages, tools):
        self.calls.append({"system": system, "messages": list(messages), "tools": tuple(tools)})
        if self._turns:
            return self._turns.pop(0)
        # Exhausted script -> a safe tool-free answer (keeps a mis-scripted test from hanging).
        return ModelTurn(text="done", tool_calls=(), model="fake-model")


class _FakeTool(Tool):
    """A tool with a fixed name that returns canned output or raises."""

    def __init__(self, name, *, output="ok", raises=None) -> None:
        self._name = name
        self._output = output
        self._raises = raises
        self.calls: list[dict] = []

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self._name, description="test tool", input_schema={"type": "object"})

    def run(self, arguments: dict) -> str:
        self.calls.append(arguments)
        if self._raises is not None:
            raise self._raises
        return self._output


def _call(name, arguments=None, call_id="c1"):
    return ToolCall(id=call_id, name=name, arguments=arguments or {})


# --- Direct answer (no tools) -----------------------------------------------------------------


def test_answers_directly_without_calling_a_tool():
    model = _ScriptedModel([ModelTurn(text="42", tool_calls=(), model="m1")])
    result = RunResearch(model, [_FakeTool("echo")]).execute("what is the answer?")
    assert result.answer == "42"
    assert result.steps == ()
    assert result.model == "m1"
    assert len(model.calls) == 1


# --- One tool round, then an answer ------------------------------------------------------------


def test_runs_a_requested_tool_and_feeds_the_result_back():
    tool = _FakeTool("echo", output="echoed-value")
    model = _ScriptedModel(
        [
            ModelTurn("let me check", (_call("echo", {"x": 1}),), model="m1"),
            ModelTurn("the value is echoed-value", (), model="m1"),
        ]
    )
    result = RunResearch(model, [tool]).execute("look it up")

    assert result.answer == "the value is echoed-value"
    assert tool.calls == [{"x": 1}]
    assert len(result.steps) == 1
    step = result.steps[0]
    assert (step.tool, step.arguments, step.output, step.is_error) == (
        "echo",
        {"x": 1},
        "echoed-value",
        False,
    )
    # The second model call sees the running transcript: user, assistant(tool_use), tool_results.
    second = model.calls[1]["messages"]
    assert [type(m).__name__ for m in second] == [
        "UserMessage",
        "AssistantMessage",
        "ToolResultsMessage",
    ]


def test_runs_multiple_tool_calls_in_one_turn():
    a, b = _FakeTool("a", output="A"), _FakeTool("b", output="B")
    model = _ScriptedModel(
        [
            ModelTurn(
                "checking both",
                (_call("a", {"n": 1}, "c1"), _call("b", {"n": 2}, "c2")),
                model="m1",
            ),
            ModelTurn("done", (), model="m1"),
        ]
    )
    result = RunResearch(model, [a, b]).execute("compare a and b")
    assert [s.tool for s in result.steps] == ["a", "b"]
    assert a.calls == [{"n": 1}] and b.calls == [{"n": 2}]


# --- Recovery: unknown tool, and a tool that raises --------------------------------------------


def test_unknown_tool_becomes_an_error_outcome_not_a_crash():
    model = _ScriptedModel(
        [
            ModelTurn("try it", (_call("does_not_exist"),), model="m1"),
            ModelTurn("recovered", (), model="m1"),
        ]
    )
    result = RunResearch(model, [_FakeTool("echo")]).execute("q")
    assert result.answer == "recovered"
    assert len(result.steps) == 1
    assert result.steps[0].is_error is True
    assert "Unknown tool" in result.steps[0].output


def test_a_raising_tool_becomes_an_error_outcome_not_a_crash():
    boom = _FakeTool("boom", raises=RuntimeError("kaboom"))
    model = _ScriptedModel(
        [
            ModelTurn("run boom", (_call("boom"),), model="m1"),
            ModelTurn("handled", (), model="m1"),
        ]
    )
    result = RunResearch(model, [boom]).execute("q")
    assert result.answer == "handled"
    assert result.steps[0].is_error is True
    assert "failed" in result.steps[0].output


# --- The step budget bounds the loop -----------------------------------------------------------


def test_forces_a_final_tool_free_turn_when_the_budget_is_spent():
    # The model keeps asking for tools; max_steps=2 caps the loop, then one final turn is forced
    # with NO tools on offer so the read still resolves to an answer.
    model = _ScriptedModel(
        [
            ModelTurn("step 1", (_call("echo", call_id="c1"),), model="m1"),
            ModelTurn("step 2", (_call("echo", call_id="c2"),), model="m1"),
            ModelTurn("final answer from what I have", (), model="m1"),
        ]
    )
    result = RunResearch(model, [_FakeTool("echo")], max_steps=2).execute("q")
    assert result.answer == "final answer from what I have"
    assert len(model.calls) == 3  # 2 budgeted turns + 1 forced final
    assert model.calls[-1]["tools"] == ()  # the forced turn offers no tools
    assert len(result.steps) == 2


def test_empty_forced_answer_falls_back_to_a_message():
    model = _ScriptedModel(
        [
            ModelTurn("step 1", (_call("echo"),), model="m1"),
            ModelTurn("", (), model="m1"),  # forced final returns nothing usable
        ]
    )
    result = RunResearch(model, [_FakeTool("echo")], max_steps=1).execute("q")
    assert result.answer == _EMPTY_ANSWER_FALLBACK


# --- Input validation --------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_a_blank_question_is_rejected(blank):
    model = _ScriptedModel([ModelTurn("x", (), model="m1")])
    with pytest.raises(ValueError):
        RunResearch(model, [_FakeTool("echo")]).execute(blank)
    assert model.calls == []  # never reached the model
