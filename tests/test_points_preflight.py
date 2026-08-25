"""Offline tests for resolving leaf weights before the solutions stage runs.

Some specs cannot be weighted at all: a question printed as 10 points whose
parts are printed as 4 and 4 contradicts itself, and no amount of solving will
settle it. ``stage_rubric`` has always caught that, but only after every
solution has been generated — on a 35-leaf practice test, twelve minutes of
model calls spent to learn something decidable the moment the spec exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autograder.config import RunConfig
from autograder.models import AssignmentSpec, Problem, ProblemType
from autograder.orchestrator import Pipeline
from autograder.rubric import PointAllocationError, check_point_allocation


def _contradictory_printed_totals() -> AssignmentSpec:
    """Question 21 is printed at 10 points; its two parts are printed at 4 each.
    No derivation can reconcile that, so it is unresolvable by construction."""
    return AssignmentSpec(
        title="Contradictory", total_points=None, n_pages=8,
        problems=[
            Problem(id="21", label="21.", prompt="stem", type=ProblemType.container,
                    points=10.0, pages=[8],
                    children=[
                        Problem(id="21A", label="(a)", prompt="find v", points=4.0, pages=[8]),
                        Problem(id="21B", label="(b)", prompt="find a", points=4.0, pages=[8]),
                    ]),
        ],
    )


def _unpriced_leaves_under_priced_parents() -> AssignmentSpec:
    """The shape of the AP Physics 1 practice test: 20 unpriced MC items, plus
    free-response parents carrying the only printed values."""
    problems: list[Problem] = [
        Problem(id=str(i), label=f"{i}.", prompt="pick one",
                type=ProblemType.multiple_choice, pages=[1 + i // 3])
        for i in range(1, 21)
    ]
    problems.append(
        Problem(id="21", label="21.", prompt="stem", type=ProblemType.container,
                points=10.0, pages=[8],
                children=[
                    Problem(id="21A", label="(a)", prompt="find v", pages=[8]),
                    Problem(id="21B", label="(b)", prompt="find a", pages=[8]),
                ])
    )
    return AssignmentSpec(title="Practice Test", total_points=None, n_pages=8,
                          problems=problems)


def _all_leaves_priced() -> AssignmentSpec:
    return AssignmentSpec(
        title="Quiz", total_points=6.0, n_pages=1,
        problems=[
            Problem(id="1", label="1.", prompt="a", points=3.0, pages=[1]),
            Problem(id="2", label="2.", prompt="b", points=3.0, pages=[1]),
        ],
    )


# -- the check itself -------------------------------------------------------


def test_the_ap_shape_now_passes_the_preflight():
    """Priced parents over unpriced leaves used to be the blocking case;
    deriving the split settles it without a teacher rubric."""
    check_point_allocation(_unpriced_leaves_under_priced_parents())


def test_contradictory_printed_totals_cannot_be_weighted():
    with pytest.raises(PointAllocationError, match="printed parent total"):
        check_point_allocation(_contradictory_printed_totals())


def test_fully_priced_leaves_resolve():
    check_point_allocation(_all_leaves_priced())


def test_a_spec_with_no_printed_points_anywhere_resolves():
    """Nothing printed anywhere is unambiguous: every leaf weighs the same."""
    spec = AssignmentSpec(
        title="Worksheet", total_points=None, n_pages=1,
        problems=[Problem(id=str(i), label=f"{i}.", prompt="q", pages=[1]) for i in (1, 2, 3)],
    )
    check_point_allocation(spec)


# -- wiring into the run ----------------------------------------------------


def _explode(*args, **kwargs):
    raise AssertionError("the solutions stage must not run before weights resolve")


@pytest.fixture()
def graded_run(tmp_path: Path, tiny_pdf: Path):
    submissions = tmp_path / "submissions"
    submissions.mkdir()
    (submissions / "alice.pdf").write_bytes(tiny_pdf.read_bytes())
    out = tmp_path / "run"
    return Pipeline(RunConfig(api_key=None), tiny_pdf, out), submissions, out


def test_grade_stops_before_solving_when_weights_cannot_resolve(graded_run, monkeypatch):
    pipe, submissions, out = graded_run
    monkeypatch.setattr("autograder.orchestrator.build_spec",
                        lambda *a, **k: _contradictory_printed_totals())
    monkeypatch.setattr(Pipeline, "stage_solutions", _explode)

    with pytest.raises(PointAllocationError, match="printed parent total"):
        pipe.run_grade([submissions], None, None, None)

    assert (out / "assignment_spec.json").exists(), "the spec is still worth keeping"
    assert not (out / "solutions_manual.json").exists()


def test_grade_checks_a_cached_spec_without_rebuilding_it(graded_run, monkeypatch):
    """The wasteful re-run is the second one, against a spec already on disk."""
    pipe, submissions, out = graded_run
    out.mkdir(parents=True, exist_ok=True)
    (out / "assignment_spec.json").write_text(
        _contradictory_printed_totals().model_dump_json(), encoding="utf-8"
    )
    monkeypatch.setattr("autograder.orchestrator.build_spec", _explode)
    monkeypatch.setattr(Pipeline, "stage_solutions", _explode)

    with pytest.raises(PointAllocationError, match="printed parent total"):
        pipe.run_grade([submissions], None, None, None)


def test_a_supplied_teacher_rubric_defers_to_the_rubric_stage(graded_run, monkeypatch, tmp_path):
    """A teacher rubric may carry the missing weights, so the preflight stands down."""
    pipe, submissions, out = graded_run
    rubric_path = tmp_path / "rubric.json"
    rubric_path.write_text(json.dumps({"assignment_title": "Practice Test", "problems": []}),
                           encoding="utf-8")
    monkeypatch.setattr("autograder.orchestrator.build_spec",
                        lambda *a, **k: _contradictory_printed_totals())

    reached = []

    def record(self, spec, solutions_path):
        reached.append(spec)
        raise RuntimeError("stop here")

    monkeypatch.setattr(Pipeline, "stage_solutions", record)

    with pytest.raises(RuntimeError, match="stop here"):
        pipe.run_grade([submissions], None, rubric_path, None)

    assert len(reached) == 1, "solving must be reached when a teacher rubric was supplied"


def test_the_preflight_states_the_assumption_it_made(caplog):
    """A derived weight is an inference, not a printed fact, so the run says so
    before anything expensive depends on it."""
    with caplog.at_level("INFO", logger="autograder"):
        check_point_allocation(_unpriced_leaves_under_priced_parents())

    line = "\n".join(caplog.messages)
    assert "derived weights for 22 unpriced" in line
    assert "20 at 1" in line and "2 at 5" in line
    assert "30 points" in line


def test_no_assumption_is_announced_when_every_weight_was_printed(caplog):
    with caplog.at_level("INFO", logger="autograder"):
        check_point_allocation(_all_leaves_priced())
    assert not [m for m in caplog.messages if "derived weights" in m]
