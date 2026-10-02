"""Offline tests for the standalone student report script."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    path = ROOT / "scripts" / "student_report.py"
    spec = importlib.util.spec_from_file_location("student_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves string annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sr = _load_script()

SPEC_PROBLEMS = [
    {"id": "1", "label": "1.", "type": "multiple_choice", "prompt": "Pick one.", "choices": ["(A) x", "(B) y"]},
    {"id": "2", "label": "2.", "type": "multiple_choice", "prompt": "Pick another."},
    {"id": "4", "label": "Questions 4\u20135", "type": "container", "children": [
        {"id": "4a", "label": "4.", "type": "multiple_choice"},
        {"id": "4b", "label": "5.", "type": "multiple_choice"},
    ]},
    {"id": "9", "label": "9.", "type": "container", "children": [
        {"id": "9a", "label": "A.", "type": "numeric", "prompt": "Calculate the speed."},
        {"id": "9b", "label": "B.", "type": "free_response"},
    ]},
    {"id": "13", "label": "13.", "type": "container", "children": [
        {"id": "13a", "label": "A.", "type": "container", "children": [
            {"id": "13a.i", "label": "i.", "type": "derivation"},
            {"id": "13a.ii", "label": "ii.", "type": "derivation"},
        ]},
    ]},
    {"id": "21", "label": "21.", "type": "container", "children": [
        {"id": "21i", "label": "(i)", "type": "numeric"},
    ]},
    {"id": "12c", "label": "", "type": "numeric"},
]


def test_display_labels_follow_printed_numbering() -> None:
    assert sr.display_labels(SPEC_PROBLEMS) == {
        "1": "1", "2": "2", "4a": "4", "4b": "5", "9a": "9(a)", "9b": "9(b)",
        "13a.i": "13(a)(i)", "13a.ii": "13(a)(ii)", "21i": "21(i)", "12c": "12(c)",
    }


def test_spec_leaves_skip_containers_and_keep_order() -> None:
    assert list(sr.spec_leaves(SPEC_PROBLEMS)) == ["1", "2", "4a", "4b", "9a", "9b", "13a.i", "13a.ii", "21i", "12c"]


def test_find_student_dir_matches_naming_styles(tmp_path: Path) -> None:
    run = tmp_path / "Homework 3"
    for name in ("2026_Summer_Physics_-_Homework_3_-_Avery_Stone", "RIVERA_BLAKE", "X_-_Ada_Avery_Nolan"):
        (run / "students" / name).mkdir(parents=True)
        (run / "students" / name / "grades.json").write_text("{}")
    assert sr.find_student_dir(run, "Avery Stone").name.endswith("Avery_Stone")
    assert sr.find_student_dir(run, "Blake Rivera").name == "RIVERA_BLAKE"
    assert sr.find_student_dir(run, "Ada Nolan").name == "X_-_Ada_Avery_Nolan"
    assert sr.find_student_dir(run, "Casey Morgan") is None
    with pytest.raises(sr.AmbiguousStudent):
        sr.find_student_dir(run, "Avery")


@pytest.mark.parametrize(
    ("folder", "expected"),
    [("Fall 2027 Intro Physics", ("Intro Physics", "Fall 2027")),
     ("fall 2027 Chemistry", ("Chemistry", "Fall 2027")),
     ("Physics Club", ("Physics Club", ""))],
)
def test_parse_course_term(folder: str, expected: tuple[str, str]) -> None:
    assert sr.parse_course_term(folder) == expected


def test_small_formatting_helpers() -> None:
    student_id = "2026 Summer Physics - Homework 11 (Gravity & Mass) - Avery Stone"
    assert sr.topic_from_student_id(student_id) == "Gravity & Mass"
    assert sr.topic_from_student_id("2026 Summer Physics - Practice Test 2 - Avery Stone") == ""
    assert sr.covers_line(["Homework 10", "Homework 11", "Homework 13", "Practice Test 2"]) == (
        "Homework 10, 11, 13 and Practice Test 2")
    assert sr.covers_line(["Final Exam"]) == "Final Exam"
    assert sr.percent(19, 27) == 70 and sr.percent(1, 8) == 13 and sr.percent(1, 200) == 1
    assert sr.fmt_points(19.0) == "19" and sr.fmt_points(2.5) == "2.5" and sr.fmt_points(1.25) == "1.25"
    assert sr.normalize_assignment("HW10") == sr.normalize_assignment("Homework 10")
    assert sr.normalize_assignment("pt 2") == sr.normalize_assignment("Practice Test 2")
    assert sr.latex_escape("Gravity & Centre_of #1") == r"Gravity \& Centre\_of \#1"
    names = ["Practice Test 1", "Homework 10", "Final Exam", "Homework 2"]
    assert sorted(names, key=sr.assignment_sort_key) == ["Homework 2", "Homework 10", "Practice Test 1", "Final Exam"]


# ---------------------------------------------------------------------------
# A small synthetic course


def _problem(awarded: float, possible: float, status: str = "answered", criteria=None, **extra) -> dict:
    criteria = criteria or [{"criterion_id": "x.c1", "awarded": awarded, "possible": possible,
                             "justification": "Student chose (B)."}]
    return {"awarded": awarded, "possible": possible, "status": status, "criteria": criteria,
            "feedback": "You chose (B).", "needs_review": False, "integrity_flags": [],
            "processing_status": "complete", "failure": None, **extra}


def _make_course(tmp_path: Path) -> Path:
    runs = tmp_path / "Summer 2026 Physics Demo" / "runs"
    run = runs / "Homework 3"
    student = run / "students" / "2026_Summer_Physics_Demo_-_Homework_3_Vectors_-_Avery_Stone"
    student.mkdir(parents=True)
    problems = [
        {"id": "1", "label": "1.", "type": "multiple_choice", "prompt": "Which vector?"},
        {"id": "2", "label": "2.", "type": "multiple_choice", "prompt": "Which force?"},
        {"id": "5", "label": "5.", "type": "container", "children": [
            {"id": "5a", "label": "A.", "type": "diagram"},
            {"id": "5b", "label": "B.", "type": "numeric"},
            {"id": "5c", "label": "C.", "type": "numeric"},
        ]},
    ]
    (run / "assignment_spec.json").write_text(json.dumps({"title": "HW3", "problems": problems}))
    (run / "rubric.json").write_text(json.dumps({"problems": [
        {"problem_id": "5a", "grading_notes": "Award each 0.5 independently."}]}))
    (run / "solutions_manual.json").write_text(json.dumps({"solutions": {"1": {"final_answer": "B"}}}))
    grades = {
        "student_id": "2026 Summer Physics Demo - Homework 3 (Vectors) - Avery Stone",
        "total_awarded": 3.5, "total_possible": 7.0, "score_complete": True,
        "problems": {
            "1": _problem(1, 1),
            "2": _problem(0, 1, needs_review=True, review_reason="borderline"),
            "5a": _problem(0, 1),
            "5b": _problem(2.5, 3, criteria=[
                {"criterion_id": "5b.c1", "awarded": 1.5, "possible": 1.5, "justification": "setup"},
                {"criterion_id": "5b.c2", "awarded": 1.0, "possible": 1.5, "justification": "answer"},
                {"criterion_id": "5b.c3", "awarded": 0.0, "possible": 0.0, "justification": "units"}]),
            "5c": _problem(0, 1, status="blank"),
        },
    }
    (student / "grades.json").write_text(json.dumps(grades))
    return runs


def _skeleton(tmp_path: Path, *extra: str) -> Path:
    runs = _make_course(tmp_path)
    workdir = tmp_path / "work"
    assert sr.main(["skeleton", "--runs", str(runs), "--student", "Avery Stone", "--workdir", str(workdir),
                    *extra]) == 0
    return workdir


PROSE = "You set this up the right way, and the result follows directly from Newton's second law."


def _fill(workdir: Path) -> None:
    for path in [*(workdir / "sections").glob("*.tex"), workdir / "cover_note.tex"]:
        text = path.read_text()
        text = re.sub(r"\\pt\{([^}]*)\}\{([^}]*)\} % TODO", r"\\pt{\1}{\2} Correct setup.", text)
        text = re.sub(r"^% TODO.*$", PROSE + " " + PROSE + " " + PROSE, text, flags=re.M)
        path.write_text(text)


def test_skeleton_writes_exact_scores_labels_and_blanks(tmp_path: Path) -> None:
    workdir = _skeleton(tmp_path, "--assignment", "HW3", "--assignment", "Homework 12")
    manifest = json.loads((workdir / "manifest.json").read_text())
    assert (manifest["course"], manifest["term"]) == ("Physics Demo", "Summer 2026")
    assert manifest["missing"] == ["Homework 12"]
    section = (workdir / "sections" / "homework_3.tex").read_text()
    assert r"\assignment{Homework 3}{Vectors}{3.5}{7}" in section
    assert r"\begin{problem}{1}{1}{1}{1}" in section
    assert r"\begin{problem}{5b}{5(b)}{2.5}{3}" in section
    assert r"\pt{1.5}{1.5} % TODO" in section and r"\pt{1}{1.5} % TODO" in section
    assert r"\pt{0}{0}" not in section
    assert r"\blankprob{5c}{5(c)}{1}" in section
    assert section.index(r"\groupheading{Multiple Choice}") < section.index(r"\groupheading{Free Response}")
    notes = (workdir / "teacher_notes.md").read_text()
    assert "Homework 12" in notes and "queued for review: borderline" in notes and "5a scored 0/1" in notes
    digest = (workdir / "sources" / "homework_3.md").read_text()
    assert "Answer key: B" in digest and "Which vector?" in digest


def test_check_rejects_placeholders_then_passes_once_filled(tmp_path: Path, capsys) -> None:
    workdir = _skeleton(tmp_path)
    assert sr.main(["check", "--workdir", str(workdir)]) == 1
    assert "unfilled TODO" in capsys.readouterr().out
    _fill(workdir)
    assert sr.main(["check", "--workdir", str(workdir)]) == 0


@pytest.mark.parametrize(
    ("before", "after", "message"),
    [(r"\begin{problem}{1}{1}{1}{1}", r"\begin{problem}{1}{1}{0}{1}", "1 shows 0/1"),
     (r"\pt{1}{1.5}", r"\pt{1.5}{1.5}", "breakdown adds to 3/3"),
     (r"\blankprob{5c}{5(c)}{1}", "", "missing=['5c']"),
     ("Newton's second law.", "the rubric.", "forbidden (rubric)"),
     ("Newton's second law.", "Newton's law \u2014 again.", "forbidden (em dash)"),
     ("Newton's second law.", "the student's law.", "third-person"),
     ("Newton's second law.", "a speed of 3 m/s\u00b2.", "write"),
     ("Newton's second law.", 'the "second" law.', "straight double quote"),
     (r"\assignment{Homework 3}{Vectors}{3.5}{7}", r"\assignment{Homework 3}{Vectors}{4}{7}", "total 4/7")],
)
def test_check_catches_tampering(tmp_path: Path, capsys, before: str, after: str, message: str) -> None:
    workdir = _skeleton(tmp_path)
    _fill(workdir)
    section = workdir / "sections" / "homework_3.tex"
    text = section.read_text()
    assert before in text
    section.write_text(text.replace(before, after, 1))
    assert sr.main(["check", "--workdir", str(workdir), "--section", "homework_3"]) == 1
    assert message in capsys.readouterr().out


def test_skeleton_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    workdir = _skeleton(tmp_path)
    with pytest.raises(SystemExit):
        sr.main(["skeleton", "--runs", str(tmp_path / "Summer 2026 Physics Demo" / "runs"), "--student",
                 "Avery Stone", "--workdir", str(workdir)])


def _tex_ready() -> bool:
    if not all(shutil.which(t) for t in ("pdflatex", "pdftotext", "pdfinfo", "kpsewhich")):
        return False
    found = subprocess.run(["kpsewhich", "newpxtext.sty", "lastpage.sty"], capture_output=True, text=True)
    return len(found.stdout.split()) == 2


@pytest.mark.skipif(not _tex_ready(), reason="needs pdflatex with newpx and lastpage, plus poppler")
def test_build_compiles_scrubs_and_delivers(tmp_path: Path) -> None:
    workdir = _skeleton(tmp_path)
    _fill(workdir)
    dest = tmp_path / "out"
    assert sr.main(["build", "--workdir", str(workdir), "--dest", str(dest)]) == 0
    pdf = dest / "Avery Stone - Physics Demo Grade Report.pdf"
    text = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    assert "Homework 3" in text and "3.5 / 7" in text and "50%" in text and "No work submitted." in text
    assert (dest / "source" / "sections" / "homework_3.tex").is_file()
    assert (dest / "source" / "teacher_notes.md").is_file()
