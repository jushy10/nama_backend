from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    rubric: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Grade:
    passed: bool
    score: float
    reasoning: str


@dataclass(frozen=True)
class CaseResult:
    case: EvalCase
    answer: str
    grade: Grade
    errored: bool = False


@dataclass(frozen=True)
class EvalReport:
    results: tuple[CaseResult, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.grade.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.errored)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def average_score(self) -> float:
        return (
            sum(r.grade.score for r in self.results) / self.total if self.total else 0.0
        )

    @property
    def failures(self) -> tuple[CaseResult, ...]:
        return tuple(r for r in self.results if not r.grade.passed)

    def meets(self, threshold: float) -> bool:
        return self.pass_rate >= threshold
