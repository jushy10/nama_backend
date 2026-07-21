from app.evals.dataset import GOLDEN_CASES


def test_there_are_cases():
    assert len(GOLDEN_CASES) >= 1


def test_case_ids_are_unique():
    ids = [c.id for c in GOLDEN_CASES]
    assert len(ids) == len(set(ids))


def test_every_case_is_well_formed():
    for case in GOLDEN_CASES:
        assert case.id.strip(), "a case has a blank id"
        assert case.question.strip(), f"{case.id} has a blank question"
        assert case.rubric.strip(), f"{case.id} has a blank rubric"
        assert case.tags, f"{case.id} has no tags"
