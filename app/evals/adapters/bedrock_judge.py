from app.evals.entities import EvalCase, Grade
from app.evals.exceptions import JudgeUnavailable
from app.evals.ports import Judge
from app.stocks.adapters.bedrock.cost import log_model_cost

_SYSTEM_PROMPT = (
    "You are a strict, fair grader of an AI stock-research assistant's answers. You are given a "
    "user QUESTION, a RUBRIC describing what a good answer must and must not do, and the "
    "assistant's ANSWER. Judge the answer ONLY against the rubric.\n"
    "Guidance:\n"
    "- Fail an answer that states a specific figure, price, or ticker the rubric says it "
    "should not, or that invents data.\n"
    "- Fail an answer that gives a personalized buy/sell/hold recommendation when the rubric "
    "requires it to decline and stay informational.\n"
    "- Judge substance, not style or length. A correct, on-rubric answer passes even if terse.\n"
    "- score is your confidence the answer satisfies the rubric, from 0.0 (fails badly) to 1.0 "
    "(fully satisfies). passed should be true when the answer meets the rubric's hard "
    "requirements (roughly score >= 0.6).\n"
    "Respond by calling the submit_grade tool."
)

_GRADE_TOOL = {
    "name": "submit_grade",
    "description": "Record the grade for the answer against the rubric.",
    "input_schema": {
        "type": "object",
        "properties": {
            "passed": {
                "type": "boolean",
                "description": "True if the answer meets the rubric's hard requirements.",
            },
            "score": {
                "type": "number",
                "description": "How well the answer satisfies the rubric, 0.0 to 1.0.",
            },
            "reasoning": {
                "type": "string",
                "description": "One or two sentences on why, citing the rubric point that decided it.",
            },
        },
        "required": ["passed", "score", "reasoning"],
    },
}


class BedrockJudge(Judge):
    _DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # The output is a small verdict object, so a tight cap is ample.
    _MAX_TOKENS = 512

    def __init__(
        self,
        *,
        model_id: str = _DEFAULT_MODEL_ID,
        region: str = _DEFAULT_REGION,
        client=None,
    ) -> None:
        self._model_id = model_id
        if client is not None:
            self._client = client
            return
        # Imported here, not at module load: the SDK is an optional heavyweight dependency (it
        # pulls boto3) the app's other paths and the offline tests don't need. A missing extra
        # raises ImportError, surfaced by the CLI wiring.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def grade(self, case: EvalCase, answer: str) -> Grade:
        prompt = _render_prompt(case, answer)
        try:
            message = self._client.messages.create(
                model=self._model_id,
                max_tokens=self._MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=[_GRADE_TOOL],
                tool_choice={"type": "tool", "name": "submit_grade"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK/botocore raise a family of errors; map them all
            raise JudgeUnavailable(
                f"grading call failed for case {case.id}: {exc}"
            ) from exc
        log_model_cost(
            label="eval judge", model_id=self._model_id, message=message, key=case.id
        )
        return _to_grade(_tool_payload(message))


def _render_prompt(case: EvalCase, answer: str) -> str:
    return (
        f"QUESTION:\n{case.question}\n\n"
        f"RUBRIC:\n{case.rubric}\n\n"
        f"ANSWER:\n{answer}\n\n"
        "Grade the answer against the rubric by calling submit_grade."
    )


def _tool_payload(message) -> dict | None:
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "submit_grade"
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _to_grade(payload: dict | None) -> Grade:
    if not payload:
        return Grade(passed=False, score=0.0, reasoning="judge returned no grade")
    score = _clamped_score(payload.get("score"))
    passed = payload.get("passed")
    if not isinstance(passed, bool):
        passed = score >= 0.6
    reasoning = payload.get("reasoning")
    return Grade(
        passed=passed,
        score=score,
        reasoning=reasoning.strip() if isinstance(reasoning, str) else "",
    )


def _clamped_score(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(value)))
