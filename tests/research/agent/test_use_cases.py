import pytest

from app.domains.research.agent.entities import (
    AgentRecipe,
    ModelTurn,
    ToolCall,
    ToolMessage,
    ToolSpec,
)
from app.domains.research.agent.errors import EmptyQuestion, MissingAgentRecipe
from app.domains.research.agent.repository import AgentRecipeRepository
from app.domains.research.agent.tool import Tool
from app.domains.research.agent.use_cases import (
    _EMPTY_ANSWER_FALLBACK,
    RunResearchUseCase,
)
from app.domains.research.rate_limit_quota.use_cases import ConsumeGenerationQuota


class _FakeRecipeRepo(AgentRecipeRepository):
    def __init__(self, recipe) -> None:
        self._recipe = recipe

    def get(self, name):
        return self._recipe


def _research(model, tools, *, max_steps=6, system_prompt="You are a test agent.", quota=None):
    # The use case fetches prompt/steps from the recipe port at run time.
    recipe = AgentRecipe(
        name="research",
        system_prompt=system_prompt,
        tool_names=tuple(t.spec.name for t in tools),
        max_steps=max_steps,
        model_id="fake-model",
    )
    return RunResearchUseCase(model, tools, _FakeRecipeRepo(recipe), "research", quota=quota)


class _ScriptedModel:
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
    def __init__(self, name, *, output="ok", raises=None) -> None:
        self._name = name
        self._output = output
        self._raises = raises
        self.calls: list[dict] = []

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self._name, description="test tool", input_schema={"type": "object"})

    def run(self, arguments: dict) -> ToolMessage:
        self.calls.append(arguments)
        if self._raises is not None:
            raise self._raises
        return ToolMessage(self._output)


def _call(name, arguments=None, call_id="c1"):
    return ToolCall(id=call_id, name=name, arguments=arguments or {})


class _FakeQuotaRepo:
    def __init__(self, *, allow=True) -> None:
        self._allow = allow
        self.consumed: list[str] = []

    def try_consume(self, pool, client_key, day, limit) -> bool:
        if not self._allow:
            return False
        self.consumed.append(client_key)
        return True


def _quota(repo) -> ConsumeGenerationQuota:
    return ConsumeGenerationQuota(repo, pool="research", daily_limit=5)


# --- The per-client daily run budget -----------------------------------------------------------


def test_a_run_spends_one_from_the_client_budget():
    model = _ScriptedModel([ModelTurn(text="42", tool_calls=(), model="m1")])
    repo = _FakeQuotaRepo()
    _research(model, [_FakeTool("echo")], quota=_quota(repo)).run(
        "what is the answer?", client_id="1.2.3.4"
    )
    assert repo.consumed == ["1.2.3.4"]


def test_an_exhausted_budget_raises_before_any_model_call():
    from app.domains.shared.exceptions import QuotaExceeded

    model = _ScriptedModel([ModelTurn(text="42", tool_calls=(), model="m1")])
    use_case = _research(model, [_FakeTool("echo")], quota=_quota(_FakeQuotaRepo(allow=False)))
    with pytest.raises(QuotaExceeded):
        use_case.run("what is the answer?", client_id="1.2.3.4")
    assert model.calls == []  # denied before the first metered Bedrock call


def test_an_empty_question_never_burns_a_generation():
    repo = _FakeQuotaRepo()
    use_case = _research(_ScriptedModel([]), [_FakeTool("echo")], quota=_quota(repo))
    with pytest.raises(EmptyQuestion):
        use_case.run("   ", client_id="1.2.3.4")
    assert repo.consumed == []


# --- Direct answer (no tools) -----------------------------------------------------------------


def test_answers_directly_without_calling_a_tool():
    model = _ScriptedModel([ModelTurn(text="42", tool_calls=(), model="m1")])
    result = _research(model, [_FakeTool("echo")]).run("what is the answer?")
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
    result = _research(model, [tool]).run("look it up")

    assert result.answer == "the value is echoed-value"
    assert tool.calls == [{"x": 1}]
    assert len(result.steps) == 1
    step = result.steps[0]
    # The payload reaches the transcript as JSON — serialized once, in the loop.
    assert (step.tool, step.arguments, step.output, step.is_error) == (
        "echo",
        {"x": 1},
        '{"message": "echoed-value"}',
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
    result = _research(model, [a, b]).run("compare a and b")
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
    result = _research(model, [_FakeTool("echo")]).run("q")
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
    result = _research(model, [boom]).run("q")
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
    result = _research(model, [_FakeTool("echo")], max_steps=2).run("q")
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
    result = _research(model, [_FakeTool("echo")], max_steps=1).run("q")
    assert result.answer == _EMPTY_ANSWER_FALLBACK


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_a_blank_question_is_rejected(blank):
    model = _ScriptedModel([ModelTurn("x", (), model="m1")])
    with pytest.raises(EmptyQuestion):
        _research(model, [_FakeTool("echo")]).run(blank)
    assert model.calls == []  # never reached the model


def test_a_missing_recipe_raises_missing_agent_recipe():
    model = _ScriptedModel([ModelTurn("x", (), model="m1")])
    use_case = RunResearchUseCase(model, [_FakeTool("echo")], _FakeRecipeRepo(None), "research")
    with pytest.raises(MissingAgentRecipe):
        use_case.run("q")
    assert model.calls == []  # config is checked before any metered call


# --- The recipe drives the loop ----------------------------------------------------------------


def test_system_prompt_comes_from_the_recipe():
    model = _ScriptedModel([ModelTurn("hi", (), model="m1")])
    _research(model, [_FakeTool("echo")], system_prompt="Be terse.").run("q")
    assert model.calls[0]["system"] == "Be terse."


def test_max_steps_comes_from_the_recipe():
    model = _ScriptedModel(
        [
            ModelTurn("s1", (_call("echo", call_id="c1"),), model="m1"),
            ModelTurn("s2", (_call("echo", call_id="c2"),), model="m1"),
            ModelTurn("s3", (_call("echo", call_id="c3"),), model="m1"),
            ModelTurn("done", (), model="m1"),
        ]
    )
    result = _research(model, [_FakeTool("echo")], max_steps=3).run("q")
    assert len(model.calls) == 4  # 3 budgeted turns + 1 forced final
    assert len(result.steps) == 3


def test_a_zero_step_budget_is_floored_to_one():
    # max(1, max_steps) guards a misconfigured row: the loop always gets one real turn.
    model = _ScriptedModel(
        [
            ModelTurn("s1", (_call("echo"),), model="m1"),
            ModelTurn("done", (), model="m1"),
        ]
    )
    result = _research(model, [_FakeTool("echo")], max_steps=0).run("q")
    assert result.answer == "done"
    assert len(model.calls) == 2  # 1 (floored) budgeted turn + 1 forced final


def test_forced_final_turn_appends_the_limit_instruction():
    model = _ScriptedModel(
        [
            ModelTurn("s1", (_call("echo"),), model="m1"),
            ModelTurn("done", (), model="m1"),
        ]
    )
    _research(model, [_FakeTool("echo")], max_steps=1, system_prompt="Base.").run("q")
    final_system = model.calls[-1]["system"]
    assert final_system.startswith("Base.")
    assert "tool-call limit" in final_system


# --- Transcript and result mechanics -----------------------------------------------------------


def test_tool_specs_are_offered_on_budgeted_turns():
    model = _ScriptedModel([ModelTurn("hi", (), model="m1")])
    _research(model, [_FakeTool("echo"), _FakeTool("other")]).run("q")
    assert [spec.name for spec in model.calls[0]["tools"]] == ["echo", "other"]


def test_tool_results_are_paired_to_their_call_ids():
    model = _ScriptedModel(
        [
            ModelTurn("both", (_call("echo", call_id="c1"), _call("echo", call_id="c2")), model="m1"),
            ModelTurn("done", (), model="m1"),
        ]
    )
    _research(model, [_FakeTool("echo")]).run("q")
    results_message = model.calls[1]["messages"][-1]
    assert [outcome.call_id for outcome in results_message.outcomes] == ["c1", "c2"]


def test_model_id_is_kept_from_an_earlier_turn_when_the_final_omits_it():
    model = _ScriptedModel(
        [
            ModelTurn("s1", (_call("echo"),), model="m1"),
            ModelTurn("done", (), model=""),  # final turn reports no model id
        ]
    )
    result = _research(model, [_FakeTool("echo")]).run("q")
    assert result.model == "m1"


def test_the_answer_is_stripped():
    model = _ScriptedModel([ModelTurn("  spaced out  ", (), model="m1")])
    assert _research(model, [_FakeTool("echo")]).run("q").answer == "spaced out"


def test_steps_survive_into_the_forced_final_result():
    model = _ScriptedModel(
        [
            ModelTurn("s1", (_call("echo", {"k": "v"}),), model="m1"),
            ModelTurn("late answer", (), model="m1"),
        ]
    )
    result = _research(model, [_FakeTool("echo")], max_steps=1).run("q")
    assert result.answer == "late answer"
    assert [(s.tool, s.arguments) for s in result.steps] == [("echo", {"k": "v"})]
