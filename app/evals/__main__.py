import argparse
import json
import logging
import sys

from app.evals.adapters.judge_adapter_impl import JudgeAdapterImpl
from app.evals.adapters.answer_under_test_adapter_impl import AnswerUnderTestAdapterImpl
from app.evals.dataset import GOLDEN_CASES
from app.evals.entities import EvalCase, EvalReport
from app.evals.report import render_summary, to_dict
from app.evals.use_cases import RunEvalSuite

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.evals", description=__doc__)
    parser.add_argument(
        "--base-url", default="http://localhost:8080", help="Subject server URL."
    )
    parser.add_argument(
        "--path", default="/agents/research", help="Endpoint path to POST the question."
    )
    parser.add_argument(
        "--answer-field",
        default="answer",
        help="Response JSON key holding the answer text.",
    )
    parser.add_argument(
        "--model", default=None, help="JudgeAdapter model id (default: the Bedrock judge's)."
    )
    parser.add_argument(
        "--region", default="us-east-1", help="Bedrock region for the judge."
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.75,
        help="Minimum pass rate (0-1) for a zero exit code.",
    )
    parser.add_argument(
        "--tags",
        nargs="*",
        default=None,
        help="Only run cases carrying any of these tags (default: all cases).",
    )
    parser.add_argument(
        "--output", default=None, help="Write the JSON report to this path."
    )
    return parser.parse_args(argv)


def _select_cases(tags: list[str] | None) -> tuple[EvalCase, ...]:
    if not tags:
        return GOLDEN_CASES
    wanted = set(tags)
    return tuple(case for case in GOLDEN_CASES if wanted.intersection(case.tags))


def _build_suite(args: argparse.Namespace) -> tuple[RunEvalSuite, AnswerUnderTestAdapterImpl]:
    subject = AnswerUnderTestAdapterImpl(
        base_url=args.base_url, path=args.path, answer_field=args.answer_field
    )
    judge = (
        JudgeAdapterImpl(model_id=args.model, region=args.region)
        if args.model
        else JudgeAdapterImpl(region=args.region)
    )
    return RunEvalSuite(subject, judge), subject


def _write_output(report: EvalReport, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(to_dict(report), handle, indent=2)
    logger.info("wrote JSON report to %s", path)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)
    cases = _select_cases(args.tags)
    if not cases:
        print("No cases matched the given tags.", file=sys.stderr)
        return 2
    try:
        suite, subject = _build_suite(args)
    except ImportError:
        print(
            "The judge needs the 'bedrock' extra. Install it with: pip install -e '.[bedrock]'",
            file=sys.stderr,
        )
        return 2

    try:
        report = suite.execute(cases)
    finally:
        subject.close()

    print(render_summary(report))
    if args.output:
        _write_output(report, args.output)

    if report.meets(args.threshold):
        return 0
    print(
        f"\nFAILED: pass rate {report.pass_rate:.0%} is below the {args.threshold:.0%} threshold.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
