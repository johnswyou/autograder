"""Offline characterization tests for the repository sample generator."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from autograder.ingest import Document, discover_submissions

ROOT = Path(__file__).resolve().parents[1]


def test_sample_generator_builds_documented_ingestion_cases(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "sample"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "generate_sample.py"),
            str(sample),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assignment_path = sample / "sample_assignment.pdf"
    submissions_path = sample / "submissions"
    normalized_stdout = " ".join(result.stdout.split())
    assert "Created a synthetic, typeset demo" in normalized_stdout
    assert f"Assignment: {assignment_path}" in normalized_stdout
    assert f"Submissions: {submissions_path}" in normalized_stdout
    assert (
        "This demo exercises page and answer mapping; it does not test "
        "handwriting recognition or OCR accuracy."
    ) in normalized_stdout
    assert "Grade the demo with:" in normalized_stdout
    assert assignment_path.is_file()

    assignment = Document.from_path(assignment_path, "assignment")
    try:
        assert assignment.n_pages == 2
    finally:
        assignment.close()

    submissions = discover_submissions([submissions_path])
    assert [
        (student_id, [path.name for path in paths])
        for student_id, paths in submissions
    ] == [("jordan_lee", ["jordan_lee.pdf"])]

    submission = Document.from_paths(submissions[0][1], "submission")
    try:
        assert submission.n_pages == 4
        assert submission.page_text(2) is None
        page_three = (submission.page_text(3) or "").splitlines()
        assert "(b)" in page_three
        assert "Q4: answer is (B), gravity still acts at the top." in page_three
        assert "Problem 1(c) continued" in (submission.page_text(4) or "")
    finally:
        submission.close()


def test_documented_sample_output_is_ignored() -> None:
    ignore_lines = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "examples/sample/" in ignore_lines
