"""The evals harness: an offline-testable, LLM-as-judge quality gate for the AI answers.

An AI stock product with no measurement of its answers' quality is a liability — a prompt or
model change can silently start hallucinating figures or handing out advice it shouldn't, and
nothing catches it. This package is the missing yardstick: a small golden set of questions, each
with a rubric of what a good answer must (and must not) do, run against a *subject under test*
(any answer endpoint) and scored by a *judge* (a model grading against the rubric).

Layered like the app's slices (dependencies point inward):

* ``entities`` — the eval primitives: a case, a grade, a per-case result, the aggregate report.
  Pure data, no vendor, no framework.
* ``ports`` — ``AnswerUnderTest`` (the thing being graded) and ``Judge`` (the grader).
* ``use_cases`` — ``RunEvalSuite`` runs each case through the subject and the judge and
  aggregates the report; a subject or judge failure is recorded, never fatal.
* ``adapters`` — the concrete grader (``BedrockJudge``) and a concrete subject
  (``HttpAnswerAdapter``, which posts a question to a running endpoint). The only code that
  knows Bedrock / HTTP exists.
* ``dataset`` — the golden cases. ``report`` renders the result. ``__main__`` is the CLI that
  wires a real subject + judge and fails the process when the pass rate drops below a threshold.

The scoring policy lives in the use case, so the whole harness runs offline in the test suite
against a fake subject and a fake judge — no Bedrock, no server, no network.
"""
