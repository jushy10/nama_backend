"""Tests for the evals CLI (app.evals.__main__).

Offline: the suite is faked (via monkeypatching the CLI's ``_build_suite``), so the CLI's own
logic — tag selection, the threshold exit code, the JSON artifact, and the missing-extra path —
runs with no Bedrock and no server.
"""

import json

from app.evals import __main__ as cli
from app.evals.entities import CaseResult, EvalCase, EvalReport, Grade


class _FakeSubject:
    def close(self) -> None:
        pass


class _FakeSuite:
    def __init__(self, report: EvalReport) -> None:
        self._report = report

    def execute(self, cases) -> EvalReport:
        return self._report


def _report(*grades: bool) -> EvalReport:
    return EvalReport(
        results=tuple(
            CaseResult(
                case=EvalCase(id=f"c{i}", question="q", rubric="r", tags=("t",)),
                answer="a",
                grade=Grade(passed=passed, score=1.0 if passed else 0.0, reasoning="x"),
            )
            for i, passed in enumerate(grades)
        )
    )


def _patch_suite(monkeypatch, report: EvalReport) -> None:
    monkeypatch.setattr(
        cli, "_build_suite", lambda args: (_FakeSuite(report), _FakeSubject())
    )


# --- Tag selection -----------------------------------------------------------------------------


def test_select_cases_defaults_to_all():
    assert cli._select_cases(None) == cli.GOLDEN_CASES


def test_select_cases_filters_by_tag():
    selected = cli._select_cases(["guardrail"])
    assert selected  # at least one guardrail case exists in the golden set
    assert all("guardrail" in c.tags for c in selected)


# --- Exit codes --------------------------------------------------------------------------------


def test_passes_the_gate_with_a_zero_exit(monkeypatch, capsys):
    _patch_suite(monkeypatch, _report(True, True, True))
    assert cli.main(["--threshold", "0.75"]) == 0


def test_fails_the_gate_with_a_nonzero_exit(monkeypatch, capsys):
    _patch_suite(monkeypatch, _report(True, False, False))  # 33% pass rate
    assert cli.main(["--threshold", "0.75"]) == 1


def test_unknown_tags_select_nothing_and_exit_2(capsys):
    assert cli.main(["--tags", "no-such-tag"]) == 2


def test_a_missing_bedrock_extra_exits_2(monkeypatch, capsys):
    def _raise(_args):
        raise ImportError("no anthropic")

    monkeypatch.setattr(cli, "_build_suite", _raise)
    assert cli.main([]) == 2


def test_writes_the_json_artifact(monkeypatch, tmp_path, capsys):
    _patch_suite(monkeypatch, _report(True, True))
    out = tmp_path / "evals.json"
    cli.main(["--threshold", "0.5", "--output", str(out)])
    data = json.loads(out.read_text())
    assert data["total"] == 2 and data["passed"] == 2
