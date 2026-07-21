"""Presenters for an ``EvalReport``: a human-readable summary and a machine-readable dict.

Kept out of the entities (which stay pure data) and out of the use case (which stays policy):
rendering is an edge concern. ``render_summary`` is what the CLI prints; ``to_dict`` is the JSON
artifact a run writes for a dashboard or a diff against a previous run.
"""

from app.evals.entities import CaseResult, EvalReport


def to_dict(report: EvalReport) -> dict:
    """The report as plain JSON-serializable data: the run-level totals plus every case."""
    return {
        "total": report.total,
        "passed": report.passed,
        "failed": report.failed,
        "errored": report.errored,
        "pass_rate": round(report.pass_rate, 4),
        "average_score": round(report.average_score, 4),
        "results": [_case_to_dict(r) for r in report.results],
    }


def _case_to_dict(result: CaseResult) -> dict:
    return {
        "id": result.case.id,
        "tags": list(result.case.tags),
        "question": result.case.question,
        "answer": result.answer,
        "passed": result.grade.passed,
        "score": round(result.grade.score, 4),
        "reasoning": result.grade.reasoning,
        "errored": result.errored,
    }


def render_summary(report: EvalReport) -> str:
    """A compact text report: a per-case pass/fail table then the run-level totals. The failing
    cases carry the judge's reasoning inline so a reviewer sees *why* without opening the JSON."""
    lines = ["", "Eval results", "=" * 60]
    for result in report.results:
        mark = "PASS" if result.grade.passed else "FAIL"
        flag = " (error)" if result.errored else ""
        lines.append(
            f"[{mark}] {result.case.id:<32} score={result.grade.score:.2f}{flag}"
        )
        if not result.grade.passed and result.grade.reasoning:
            lines.append(f"       ↳ {result.grade.reasoning}")
    lines.append("-" * 60)
    lines.append(
        f"{report.passed}/{report.total} passed "
        f"(pass_rate={report.pass_rate:.0%}, avg_score={report.average_score:.2f}, "
        f"errors={report.errored})"
    )
    return "\n".join(lines)
