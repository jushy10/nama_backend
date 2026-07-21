from app.evals.entities import CaseResult, EvalReport


def to_dict(report: EvalReport) -> dict:
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
