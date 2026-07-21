class EvalError(Exception):
    pass


class SubjectUnavailable(EvalError):
    pass


class JudgeUnavailable(EvalError):
    pass
