"""Regression tests for keeping run inputs outside generated output paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autograder.config import RunConfig
from autograder.models import Rubric, SolutionsManual
from autograder.orchestrator import Pipeline
from autograder.run_state import RunBindingError


def _model_stage_reached(*args, **kwargs):
    raise AssertionError("a model stage must not run for overlapping paths")


def _assert_no_stage_artifacts(output: Path) -> None:
    binding = json.loads((output / "run_binding.json").read_text(encoding="utf-8"))
    assert binding["inputs"] == {}
    for artifact in (
        "assignment_spec.json",
        "solutions_manual.json",
        "solutions_manual.md",
        "rubric.json",
        "rubric.md",
        "students",
        "summary.csv",
        "review_queue.md",
        "run_manifest.json",
    ):
        assert not (output / artifact).exists()


def test_pipeline_rejects_assignment_inside_output_before_creating_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "run"
    assignment = output / "assignment.pdf"

    with pytest.raises(RunBindingError, match="choose a separate --out directory"):
        Pipeline(RunConfig(api_key=None), assignment, output)

    assert not output.exists()


def test_grade_rejects_output_inside_submission_directory_before_discovery(
    tmp_path: Path, tiny_pdf: Path, monkeypatch,
) -> None:
    submissions = tmp_path / "submissions"
    submissions.mkdir()
    (submissions / "alice.pdf").write_bytes(tiny_pdf.read_bytes())
    output = submissions / "generated"
    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, output)
    monkeypatch.setattr("autograder.orchestrator.discover_submissions", _model_stage_reached)
    monkeypatch.setattr("autograder.orchestrator.build_spec", _model_stage_reached)

    try:
        with pytest.raises(RunBindingError, match="choose a separate --out directory"):
            pipe.run_grade([submissions], None, None, None)
    finally:
        pipe.assignment.close()

    _assert_no_stage_artifacts(output)


def test_solve_rejects_solution_inside_output(
    tmp_path: Path, tiny_pdf: Path, monkeypatch,
) -> None:
    output = tmp_path / "run"
    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, output)
    solution = output / "answer-key.md"
    solution.write_text("Problem 1: 4", encoding="utf-8")
    monkeypatch.setattr("autograder.orchestrator.build_spec", _model_stage_reached)

    try:
        with pytest.raises(RunBindingError, match="choose a separate --out directory"):
            pipe.run_solve(solution)
    finally:
        pipe.assignment.close()

    _assert_no_stage_artifacts(output)


def test_rubric_rejects_rubric_inside_output(
    tmp_path: Path, tiny_pdf: Path, monkeypatch,
) -> None:
    output = tmp_path / "run"
    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, output)
    rubric = output / "rubric-source.md"
    rubric.write_text("Problem 1: 10 points", encoding="utf-8")
    monkeypatch.setattr("autograder.orchestrator.build_spec", _model_stage_reached)

    try:
        with pytest.raises(RunBindingError, match="choose a separate --out directory"):
            pipe.run_rubric(None, rubric, None)
    finally:
        pipe.assignment.close()

    _assert_no_stage_artifacts(output)


def test_stage_student_rejects_submission_inside_output_before_binding_or_loading(
    tmp_path: Path, tiny_pdf: Path, small_spec, monkeypatch,
) -> None:
    output = tmp_path / "run"
    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, output)
    submission = output / "alice.pdf"
    submission.write_bytes(tiny_pdf.read_bytes())
    monkeypatch.setattr("autograder.orchestrator.Document.from_paths", _model_stage_reached)
    monkeypatch.setattr("autograder.orchestrator.map_student", _model_stage_reached)

    try:
        with pytest.raises(RunBindingError, match="choose a separate --out directory"):
            pipe.stage_student(small_spec, Rubric(), SolutionsManual(), "alice", [submission])
    finally:
        pipe.assignment.close()

    _assert_no_stage_artifacts(output)


def test_sibling_input_and_output_paths_are_allowed(tmp_path: Path, tiny_pdf: Path) -> None:
    output = tmp_path / "run"

    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, output)
    pipe.assignment.close()

    assert (output / "run_binding.json").exists()
