"""Enterprise Business Rules: the evals harness's primitives.

Pure domain objects — frozen dataclasses only, no vendor SDK, no framework. An ``EvalCase`` is
one graded question with the rubric that defines a good answer; a ``Grade`` is the judge's
verdict; a ``CaseResult`` pairs a case with the answer it drew and its grade; an ``EvalReport``
aggregates the run and computes the facts a gate reads (pass rate, average score, the failures).

The aggregate facts are computed properties, not stored — the report is assembled from the
per-case results and derives everything else on access, so there's one source of truth.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalCase:
    """One graded question. ``rubric`` is the yardstick the judge scores against — what a good
    answer must contain and must avoid (a fabricated figure, a personalized buy/sell call). A
    unique ``id`` keys the case in the report; ``tags`` group cases by concern (grounding,
    guardrail, factual) so a run can be sliced or filtered."""

    id: str
    question: str
    rubric: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Grade:
    """The judge's verdict on one answer: ``passed`` (did it satisfy the rubric), a ``score`` in
    [0, 1] (how well), and ``reasoning`` (why — kept so a failure is actionable, not opaque)."""

    passed: bool
    score: float
    reasoning: str


@dataclass(frozen=True)
class CaseResult:
    """One case's outcome: the case, the answer the subject produced, and the judge's grade.
    ``errored`` marks a run-level failure — the subject or the judge itself broke — which counts
    as a fail but is distinguished from an answer the judge simply failed on the merits."""

    case: EvalCase
    answer: str
    grade: Grade
    errored: bool = False


@dataclass(frozen=True)
class EvalReport:
    """The aggregate of one suite run. Holds the per-case results and derives the run-level
    facts a quality gate reads — the pass rate it thresholds on, the mean score, and the list of
    failing cases to show."""

    results: tuple[CaseResult, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        """How many cases the judge passed."""
        return sum(1 for r in self.results if r.grade.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def errored(self) -> int:
        """How many cases failed at the run level (subject or judge broke), a subset of the
        fails — surfaced separately so a broken endpoint reads differently from a bad answer."""
        return sum(1 for r in self.results if r.errored)

    @property
    def pass_rate(self) -> float:
        """Fraction of cases passed, in [0, 1]. An empty run is 0.0 (nothing proven)."""
        return self.passed / self.total if self.total else 0.0

    @property
    def average_score(self) -> float:
        """Mean judge score across all cases, in [0, 1] (0.0 for an empty run)."""
        return (
            sum(r.grade.score for r in self.results) / self.total if self.total else 0.0
        )

    @property
    def failures(self) -> tuple[CaseResult, ...]:
        """The cases that did not pass, in run order — what a reviewer needs to look at."""
        return tuple(r for r in self.results if not r.grade.passed)

    def meets(self, threshold: float) -> bool:
        """Whether the run clears a pass-rate gate (used by the CLI to set its exit code)."""
        return self.pass_rate >= threshold
