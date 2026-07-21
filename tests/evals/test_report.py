"""Tests for the eval-report presenters (to_dict + render_summary).

Offline: builds an ``EvalReport`` from canned results and checks both renderings — the JSON dict
a run writes and the text summary the CLI prints — without a subject or a judge.
"""

from app.evals.entities import CaseResult, EvalCase, EvalReport, Grade
from app.evals.report import render_summary, to_dict


def _result(
    cid, *, passed, score, reasoning="because", errored=False, tags=()
) -> CaseResult:
    return CaseResult(
        case=EvalCase(id=cid, question="q", rubric="r", tags=tags),
        answer="an answer",
        grade=Grade(passed=passed, score=score, reasoning=reasoning),
        errored=errored,
    )


def _report() -> EvalReport:
    return EvalReport(
        results=(
            _result("a", passed=True, score=0.9, tags=("grounding",)),
            _result("b", passed=False, score=0.2, reasoning="gave advice"),
            _result(
                "c",
                passed=False,
                score=0.0,
                reasoning="judge unavailable",
                errored=True,
            ),
        )
    )


def test_to_dict_carries_totals_and_every_case():
    data = to_dict(_report())
    assert data["total"] == 3
    assert data["passed"] == 1
    assert data["failed"] == 2
    assert data["errored"] == 1
    assert data["pass_rate"] == round(1 / 3, 4)
    assert [c["id"] for c in data["results"]] == ["a", "b", "c"]
    b = next(c for c in data["results"] if c["id"] == "b")
    assert (
        b["passed"] is False and b["score"] == 0.2 and b["reasoning"] == "gave advice"
    )


def test_render_summary_shows_marks_totals_and_failure_reasons():
    text = render_summary(_report())
    assert "[PASS] a" in text
    assert "[FAIL] b" in text
    assert "[FAIL] c" in text and "(error)" in text  # errored case is flagged
    assert "gave advice" in text  # a failure's reasoning is inline
    assert "1/3 passed" in text
    assert "pass_rate=33%" in text
