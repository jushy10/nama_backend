"""Domain-level errors for the evals harness.

Expressed in the harness's own terms, independent of Bedrock or HTTP. The adapters translate a
vendor/transport failure into one of these; the use case treats either as a recorded per-case
error (a failed grade) rather than letting it sink the whole run.
"""


class EvalError(Exception):
    """Base for the evals harness's own errors."""


class SubjectUnavailable(EvalError):
    """The subject under test could not produce an answer (endpoint down, bad response)."""


class JudgeUnavailable(EvalError):
    """The judge could not grade an answer (the grading model call failed)."""
