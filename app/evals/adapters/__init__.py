"""Concrete adapters for the evals harness: the grader and the subject under test.

``bedrock_judge`` is the LLM-as-judge (the only code that knows Bedrock exists for grading);
``http_subject`` posts a question to a running answer endpoint (the only code that knows the
transport). Each implements one of the harness's ports and translates its own failures into the
harness's own errors, so the use case and the offline tests never see a vendor or a socket.
"""
