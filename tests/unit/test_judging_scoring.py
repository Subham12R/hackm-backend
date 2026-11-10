from types import SimpleNamespace

from src.judging.service import scorecard_total


def criterion(key: str, max_score: int, weight: float = 1.0):
    return SimpleNamespace(key=key, max_score=max_score, weight=weight)


def test_score_is_points_earned_out_of_the_rubric_maximum() -> None:
    # Max scores already split 100 points; equal weights mean 33 points is 33%, not the average
    # of each criterion's fraction (which made a 33/100 card read as 24-31%).
    rubric = [criterion("a", 40), criterion("b", 20), criterion("c", 20), criterion("d", 10), criterion("e", 10)]
    assert scorecard_total(rubric, {"a": 20, "b": 2, "c": 5, "d": 5, "e": 1}) == 33.0


def test_weights_scale_both_points_earned_and_the_maximum() -> None:
    rubric = [criterion("impact", 10, 2), criterion("build", 5, 1)]
    assert scorecard_total(rubric, {"impact": 8, "build": 4}) == 80.0

    # A heavily weighted criterion lifts the percentage even though it is not the lowest fraction.
    rubric = [criterion("light", 10, 1), criterion("heavy", 20, 3)]
    assert scorecard_total(rubric, {"light": 10, "heavy": 0}) == 14.29


def test_missing_scores_count_as_zero_and_an_empty_rubric_counts_as_none() -> None:
    rubric = [criterion("a", 10), criterion("b", 10)]
    assert scorecard_total(rubric, {"a": 5}) == 25.0
    assert scorecard_total([], {"a": 5}) == 0.0
