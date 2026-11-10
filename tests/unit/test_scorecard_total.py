from src.db.models.judging import EvaluationCriterion
from src.judging.service import scorecard_total


def _rubric(*items):
    return [EvaluationCriterion(key=key, label=key, max_score=max_score, weight=weight) for key, max_score, weight in items]


def test_total_is_points_out_of_points_for_any_number_of_metrics():
    # Six metrics whose maxes add up to 100: the total is the plain point sum, not an average of percentages.
    rubric = _rubric(("a", 28, 1), ("b", 27, 1), ("c", 18, 1), ("d", 13, 1), ("e", 5, 1), ("f", 9, 1))
    assert scorecard_total(rubric, {"a": 28, "b": 20, "c": 16, "d": 9, "e": 5, "f": 5}) == 83.0


def test_weight_scales_a_metrics_points():
    rubric = _rubric(("impact", 10, 2), ("execution", 5, 1))
    assert scorecard_total(rubric, {"impact": 9, "execution": 5}) == 92.0
    assert scorecard_total(rubric, {}) == 0.0
    assert scorecard_total([], {}) == 0.0


def test_default_rubric_unchanged():
    rubric = _rubric(*((key, 10, 1) for key in "abcde"))
    assert scorecard_total(rubric, dict.fromkeys("abcde", 7)) == 70.0
