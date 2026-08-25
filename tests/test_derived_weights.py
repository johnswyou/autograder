"""Offline tests for deriving leaf weights from printed parent totals.

Exams routinely print a point value for a whole question and nothing for its
parts: "15 points" next to question 22, silence next to 22a through 22e. The
autograder grades the parts, so it needs five numbers where the paper gives it
one. These tests pin the split that fills that gap, and the boundaries where it
declines to guess.
"""

from __future__ import annotations

import pytest

from autograder.models import AssignmentSpec, Problem, ProblemType, Rubric, RubricProblem
from autograder.rubric import PointAllocationError, _problem_points


def _leaf(pid: str, points: float | None = None) -> Problem:
    return Problem(id=pid, label=pid, prompt="q", type=ProblemType.numeric, points=points)


def _container(pid: str, points: float | None, children: list[Problem]) -> Problem:
    return Problem(id=pid, label=pid, prompt="stem", type=ProblemType.container,
                   points=points, children=children)


def _spec(problems: list[Problem], total: float | None = None) -> AssignmentSpec:
    return AssignmentSpec(title="Exam", total_points=total, problems=problems)


# -- splitting a printed parent total ---------------------------------------


def test_a_parent_total_splits_evenly_across_its_unpriced_parts():
    spec = _spec([_container("21", 10.0, [_leaf(f"21{c}") for c in "abcd"])])
    assert _problem_points(spec) == {"21a": 2.5, "21b": 2.5, "21c": 2.5, "21d": 2.5}


def test_a_parent_total_splits_only_what_its_priced_parts_leave():
    """22 is worth 15 and 22a is printed at 5, so 22b-e share the other 10."""
    spec = _spec([_container("22", 15.0, [_leaf("22a", 5.0), *(_leaf(f"22{c}") for c in "bcde")])])
    assert _problem_points(spec) == {"22a": 5.0, "22b": 2.5, "22c": 2.5, "22d": 2.5, "22e": 2.5}


def test_the_nearest_priced_ancestor_governs():
    """An inner priced group splits its own total, not its grandparent's."""
    spec = _spec([
        _container("3", 20.0, [
            _container("3a", 8.0, [_leaf("3a.i"), _leaf("3a.ii")]),
            _leaf("3b", 12.0),
        ]),
    ])
    assert _problem_points(spec) == {"3a.i": 4.0, "3a.ii": 4.0, "3b": 12.0}


def test_the_assignment_total_is_the_root_parent_total():
    """A paper worth 10 with two unpriced questions prints the split implicitly."""
    spec = _spec([_leaf("1"), _leaf("2")], total=10.0)
    assert _problem_points(spec) == {"1": 5.0, "2": 5.0}


# -- where it declines to divide --------------------------------------------


def test_leaves_outside_an_exhausted_total_fall_back_to_one_point_each():
    """The AP shape: the printed 45 covers Section II only, so the 20 unpriced
    multiple-choice questions are outside it and cannot be carved from it."""
    spec = _spec(
        [
            *(_leaf(str(i)) for i in range(1, 21)),
            _container("21", 10.0, [_leaf(f"21{c}") for c in "abcd"]),
            _container("22", 15.0, [_leaf(f"22{c}") for c in "abcde"]),
            _container("23", 15.0, [_leaf(f"23{c}") for c in "abcde"]),
            _leaf("24", 5.0),
        ],
        total=45.0,
    )
    points = _problem_points(spec)

    assert points["1"] == 1.0 and points["20"] == 1.0
    assert points["21a"] == 2.5
    assert points["22a"] == 3.0
    assert points["23a"] == 3.0
    assert points["24"] == 5.0
    assert sum(points.values()) == 65.0


def test_a_parent_whose_parts_already_exhaust_it_leaves_them_at_one_point():
    spec = _spec([_container("4", 6.0, [_leaf("4a", 6.0), _leaf("4b")])])
    assert _problem_points(spec) == {"4a": 6.0, "4b": 1.0}


def test_nothing_printed_anywhere_still_weighs_every_leaf_equally():
    assert _problem_points(_spec([_leaf("1"), _leaf("2")])) == {"1": 1.0, "2": 1.0}


def test_every_leaf_printed_still_wins_before_any_derivation():
    spec = _spec([_leaf("1", 3.0), _leaf("2", 7.0)], total=10.0)
    assert _problem_points(spec) == {"1": 3.0, "2": 7.0}


# -- what still fails -------------------------------------------------------


def test_priced_parts_that_contradict_their_parent_are_still_an_error():
    spec = _spec([_container("5", 10.0, [_leaf("5a", 4.0), _leaf("5b", 4.0)])])
    with pytest.raises(PointAllocationError, match="printed parent total"):
        _problem_points(spec)


def test_a_supplied_rubric_outranks_a_derived_split():
    """The teacher's weights are the point of supplying a rubric."""
    spec = _spec([_container("21", 10.0, [_leaf("21a"), _leaf("21b")])])
    rubric = Rubric(title="Exam", problems=[
        RubricProblem(problem_id="21a", points=8.0, criteria=[]),
        RubricProblem(problem_id="21b", points=2.0, criteria=[]),
    ])
    assert _problem_points(spec, rubric) == {"21a": 8.0, "21b": 2.0}


def test_a_supplied_rubric_that_contradicts_a_printed_leaf_is_still_an_error():
    spec = _spec([_leaf("1", 3.0), _leaf("2", 7.0)], total=10.0)
    rubric = Rubric(title="Exam", problems=[
        RubricProblem(problem_id="1", points=5.0, criteria=[]),
        RubricProblem(problem_id="2", points=5.0, criteria=[]),
    ])
    with pytest.raises(PointAllocationError, match="explicit leaf weight"):
        _problem_points(spec, rubric)


# -- the derivation must survive being handed back ---------------------------


def _ap_shaped_spec() -> AssignmentSpec:
    """20 unpriced multiple-choice questions plus a printed 45-point Section II."""
    return _spec(
        [
            *(_leaf(str(i)) for i in range(1, 21)),
            _container("21", 10.0, [_leaf(f"21{c}") for c in "abcd"]),
            _container("22", 15.0, [_leaf(f"22{c}") for c in "abcde"]),
            _container("23", 15.0, [_leaf(f"23{c}") for c in "abcde"]),
            _leaf("24", 5.0),
        ],
        total=45.0,
    )


def _rubric_of(points: dict[str, float]) -> Rubric:
    return Rubric(title="Exam", problems=[
        RubricProblem(problem_id=pid, points=weight, criteria=[])
        for pid, weight in points.items()
    ])


def test_a_rubric_carrying_the_derived_weights_back_resolves_identically():
    """``complete_rubric`` re-resolves the allocation against the rubric it has
    just generated from it. The weights are the ones ``_problem_points`` chose a
    moment earlier, so the second pass must reach the same verdict as the first
    rather than reject them against a printed total that never covered them."""
    spec = _ap_shaped_spec()
    derived = _problem_points(spec)

    assert _problem_points(spec, _rubric_of(derived)) == derived


def test_an_enclosing_total_that_cannot_pay_stays_unenforced_for_any_supplier():
    """Which leaves the paper's numbers cover is a fact about the paper. A
    weight the printed 45 could not have produced is no more checkable against
    it because a rubric supplied it instead of the derivation."""
    spec = _ap_shaped_spec()
    weights = {**_problem_points(spec), "1": 2.0}

    assert _problem_points(spec, _rubric_of(weights))["1"] == 2.0


def test_a_rubric_that_overspends_a_payable_printed_parent_is_still_an_error():
    """Loosening the unpayable case must not loosen the case the paper does
    cover: 21 prints 10 and its four parts are entirely inside that number."""
    spec = _ap_shaped_spec()
    weights = {**_problem_points(spec), "21a": 4.0}

    with pytest.raises(PointAllocationError, match="printed parent total for '21'"):
        _problem_points(spec, _rubric_of(weights))
