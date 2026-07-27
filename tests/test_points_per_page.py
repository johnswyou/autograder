"""Offline tests for the standalone points-per-page reporting script."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    path = ROOT / "scripts" / "points_per_page.py"
    spec = importlib.util.spec_from_file_location("points_per_page", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves string annotations (the script uses `from __future__
    # import annotations`) through sys.modules[cls.__module__], so the module
    # must be registered before it executes or every dataclass raises.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ppp = _load_script()


def test_flatten_leaves_returns_only_terminal_problems() -> None:
    problems = [
        {"id": "1", "pages": [1], "children": []},
        {
            "id": "4-5",
            "pages": [1],
            "children": [
                {"id": "4", "pages": [1], "children": []},
                {"id": "5", "pages": [1], "children": []},
            ],
        },
        {"id": "7", "pages": [3, 4], "children": []},
        {
            "id": "16",
            "pages": [6, 7],
            "children": [
                {
                    "id": "16a",
                    "pages": [6],
                    "children": [
                        {"id": "16a.i", "pages": [6], "children": []},
                        {"id": "16a.ii", "pages": [6], "children": []},
                    ],
                },
                {"id": "16d", "pages": [7], "children": []},
            ],
        },
    ]

    assert ppp.flatten_leaves(problems) == {
        "1": [1],
        "4": [1],
        "5": [1],
        "7": [3, 4],
        "16a.i": [6],
        "16a.ii": [6],
        "16d": [7],
    }


def test_flatten_leaves_tolerates_missing_and_malformed_pages() -> None:
    problems = [
        {"id": "1", "children": []},
        {"id": "2", "pages": None, "children": []},
        {"id": "3", "pages": [2, "x", 3], "children": []},
        {"id": "4", "pages": [True, 5], "children": []},
    ]

    assert ppp.flatten_leaves(problems) == {"1": [], "2": [], "3": [2, 3], "4": [5]}


def test_flatten_leaves_skips_childless_container_problems() -> None:
    """A container without children is not gradable, so it carries no points.

    ``autograder/assignment.py`` forces ``type="container"`` onto any node that
    has children, but never clears it from a node that has none. Such a node
    survives into ``assignment_spec.json`` yet is excluded from
    ``AssignmentSpec.leaves()`` (see ``Problem.is_leaf``), so it can never
    appear in ``grades.json``.
    """
    problems = [
        {"id": "1", "type": "free_response", "pages": [1], "children": []},
        {"id": "2", "type": "container", "pages": [1], "children": []},
        {
            "id": "3",
            "type": "container",
            "pages": [2],
            "children": [{"id": "3a", "type": "numeric", "pages": [2], "children": []}],
        },
    ]

    assert ppp.flatten_leaves(problems) == {"1": [1], "3a": [2]}


def _grades(problems: dict[str, tuple[float, float]], **overrides: object) -> dict:
    total_awarded = sum(awarded for awarded, _ in problems.values())
    total_possible = sum(possible for _, possible in problems.values())
    payload: dict = {
        "student_id": "Student A",
        "total_awarded": total_awarded,
        "total_possible": total_possible,
        "score_complete": True,
        "problems": {
            pid: {"awarded": awarded, "possible": possible}
            for pid, (awarded, possible) in problems.items()
        },
    }
    payload.update(overrides)
    return payload


def test_accumulate_buckets_points_by_assignment_page() -> None:
    leaf_pages = {"1": [1], "2": [1], "10": [3]}
    grades = _grades({"1": (1.0, 1.0), "2": (0.0, 1.0), "10": (0.5, 2.0)})

    row = ppp.accumulate(grades, leaf_pages)

    assert row.student_id == "Student A"
    assert row.per_page[1].awarded == 1.0
    assert row.per_page[1].possible == 2.0
    assert row.per_page[3].awarded == 0.5
    assert row.per_page[3].possible == 2.0
    assert row.unknown.possible == 0.0
    assert row.warnings == []


def test_accumulate_attributes_a_multi_page_problem_to_its_first_page() -> None:
    row = ppp.accumulate(_grades({"16": (3.0, 4.0)}), {"16": [6, 7]})

    assert row.per_page[6].awarded == 3.0
    assert row.per_page[6].possible == 4.0
    assert 7 not in row.per_page


def test_accumulate_sends_a_pageless_problem_to_the_unknown_bucket() -> None:
    row = ppp.accumulate(_grades({"9": (1.0, 2.0)}), {"9": []})

    assert row.unknown.awarded == 1.0
    assert row.unknown.possible == 2.0
    assert row.per_page == {}
    assert "9" in row.warnings[0]


def test_accumulate_sends_an_unspecified_problem_to_the_unknown_bucket() -> None:
    row = ppp.accumulate(_grades({"99": (1.0, 2.0)}), {"1": [1]})

    assert row.unknown.awarded == 1.0
    assert row.unknown.possible == 2.0
    assert "99" in row.warnings[0]


def test_accumulate_records_an_incomplete_score() -> None:
    grades = _grades({"1": (1.0, 1.0)}, score_complete=False)

    assert ppp.accumulate(grades, {"1": [1]}).score_complete is False


def test_accumulate_falls_back_to_processed_awarded_for_an_incomplete_score() -> None:
    """An incomplete run records ``total_awarded: null``, not a zero.

    ``aggregate_student_grade`` sets ``total_awarded`` to None exactly when some
    problem failed processing, keeping the graded-so-far figure in
    ``processed_awarded``. Reading that null as 0.0 would strand the page
    buckets above the row's own recorded total.
    """
    grades = _grades(
        {"1": (3.0, 4.0)},
        score_complete=False,
        total_awarded=None,
        processed_awarded=3.0,
    )

    row = ppp.accumulate(grades, {"1": [1]})

    assert row.total_awarded == 3.0
    assert row.per_page[1].awarded == 3.0
    assert row.score_complete is False


def test_accumulate_prefers_the_recorded_total_over_the_processed_figure() -> None:
    """The fallback applies only to a null total, never to a complete score."""
    grades = _grades({"1": (1.0, 2.0)}, total_awarded=1.0, processed_awarded=99.0)

    assert ppp.accumulate(grades, {"1": [1]}).total_awarded == 1.0


def test_reconcile_accepts_totals_within_floating_point_tolerance() -> None:
    """0.1 + 0.2 != 0.3 in binary floating point, so this passes only under TOLERANCE."""
    grades = _grades({"1": (0.1, 1.0), "2": (0.2, 1.0)})
    grades["total_awarded"] = 0.3
    row = ppp.accumulate(grades, {"1": [1], "2": [2]})
    # Clear the dataclass default first, so a passing assertion below proves
    # `reconcile` assigned True rather than merely leaving the default alone.
    row.reconciled = False

    ppp.reconcile(row)

    assert row.reconciled is True
    assert row.warnings == []


def test_reconcile_flags_and_reports_a_mismatch() -> None:
    grades = _grades({"1": (1.0, 1.0)}, total_awarded=99.0)
    row = ppp.accumulate(grades, {"1": [1]})

    ppp.reconcile(row)

    assert row.reconciled is False
    # Pin the whole message: a substring check on "1" would pass no matter what
    # the computed figures were, since the recorded totals supply a "1" anyway.
    assert row.warnings == ["per-page totals 1.0000/1.0000 do not match the recorded 99.0000/1.0000"]


def test_reconcile_reports_totals_that_differ_below_six_significant_digits() -> None:
    """`:g` would round both sides to 123.457 and print a self-contradicting message."""
    grades = _grades({"1": (123.4567, 200.0)}, total_awarded=123.4561)
    row = ppp.accumulate(grades, {"1": [1]})

    ppp.reconcile(row)

    assert row.reconciled is False
    assert row.warnings == ["per-page totals 123.4567/200.0000 do not match the recorded 123.4561/200.0000"]


def test_reconcile_is_idempotent() -> None:
    """Re-running must not duplicate the warning, and a corrected row must re-validate."""
    grades = _grades({"1": (1.0, 1.0)}, total_awarded=99.0)
    row = ppp.accumulate(grades, {"1": [1]})

    ppp.reconcile(row)
    ppp.reconcile(row)

    assert row.reconciled is False
    assert len(row.warnings) == 1

    row.total_awarded = 1.0
    ppp.reconcile(row)

    assert row.reconciled is True


def test_percent_returns_none_when_no_points_are_possible() -> None:
    assert ppp.percent(0.0, 0.0) is None
    assert ppp.percent(3.0, 4.0) == 75.0


def test_csv_text_matches_the_projects_own_neutralization() -> None:
    from autograder import report

    adversarial = [
        "=cmd()",
        " +danger",
        "\t-1+2",
        "@SUM(A1)",
        "plain\x00text",
        "\x00=hidden",
        "PHYS 101 - Homework 2 (1D Kinematics) - Avery Stone",
    ]
    for value in adversarial:
        assert ppp.csv_text(value) == report.csv_text(value)


def _write_run(root: Path, students: dict[str, dict[str, tuple[float, float]]]) -> Path:
    """Build a run directory shaped like a real `autograder grade --out`."""
    run = root / "run"
    (run / "students").mkdir(parents=True)
    spec = {
        "title": "Kinematics",
        "n_pages": 3,
        "problems": [
            {"id": "1", "pages": [1], "children": []},
            {"id": "2", "pages": [1], "children": []},
            {
                "id": "12",
                "pages": [2],
                "children": [
                    {"id": "12a", "pages": [2], "children": []},
                    {"id": "12b", "pages": [2], "children": []},
                ],
            },
            {"id": "16", "pages": [3], "children": []},
        ],
    }
    (run / "assignment_spec.json").write_text(json.dumps(spec), encoding="utf-8")
    for student_id, problems in students.items():
        directory = run / "students" / student_id.replace(" ", "_")
        directory.mkdir()
        payload = _grades(problems)
        payload["student_id"] = student_id
        (directory / "grades.json").write_text(json.dumps(payload), encoding="utf-8")
    return run


def test_page_columns_cover_every_assignment_page() -> None:
    spec = {"n_pages": 4}
    assert ppp.page_columns(spec, {"1": [1], "9": [3]}) == [1, 2, 3, 4]


def test_page_columns_fall_back_to_observed_pages() -> None:
    assert ppp.page_columns({}, {"1": [1], "9": [3]}) == [1, 3]


def test_display_labels_drop_the_shared_prefix() -> None:
    ids = [
        "PHYS 101 - HW2 - Avery Stone",
        "PHYS 101 - HW2 - Blake Rivera",
    ]
    labels = ppp.display_labels(ids)

    assert labels[ids[0]] == "Avery Stone"
    assert labels[ids[1]] == "Blake Rivera"


def test_display_labels_keep_a_lone_student_readable() -> None:
    labels = ppp.display_labels(["Avery Stone"])

    assert labels["Avery Stone"] == "Avery Stone"


def test_display_labels_keep_whole_names_when_only_a_letter_is_shared() -> None:
    """A shared run that stops mid-field is not a prefix worth dropping.

    `os.path.commonprefix` compares characters, so this roster shares a bare
    "A". Trimming it would print "very Stone" and "da Nolan".
    """
    labels = ppp.display_labels(["Avery Stone", "Ada Nolan"])

    assert labels["Avery Stone"] == "Avery Stone"
    assert labels["Ada Nolan"] == "Ada Nolan"


def test_display_labels_trim_a_shared_prefix_back_to_its_last_separator() -> None:
    """The course prefix still goes; only the half-eaten first name is kept."""
    labels = ppp.display_labels(["HW2 - Avery Stone", "HW2 - Ada Nolan"])

    assert labels["HW2 - Avery Stone"] == "Avery Stone"
    assert labels["HW2 - Ada Nolan"] == "Ada Nolan"


def test_load_rows_reads_every_student(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        {
            "Avery Stone": {"1": (1.0, 1.0), "2": (0.0, 1.0), "12a": (0.5, 1.0), "16": (2.0, 2.0)},
            "Blake Rivera": {"1": (1.0, 1.0), "2": (1.0, 1.0), "12a": (1.0, 1.0), "16": (0.0, 2.0)},
        },
    )

    rows, pages = ppp.load_rows(run)

    assert pages == [1, 2, 3]
    by_id = {row.student_id: row for row in rows}
    assert by_id["Avery Stone"].per_page[1].awarded == 1.0
    assert by_id["Avery Stone"].per_page[3].awarded == 2.0
    assert by_id["Blake Rivera"].per_page[3].awarded == 0.0
    assert all(row.reconciled for row in rows)


def _rewrite_grades(run: Path, **changes: object) -> Path:
    """Overwrite one student's grades.json with the given fields replaced."""
    path = run / "students" / "Avery_Stone" / "grades.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_rows_rejects_a_spec_that_is_not_an_object(tmp_path: Path) -> None:
    """Valid JSON of the wrong shape is a named error, not an AttributeError."""
    run = _write_run(tmp_path, {"Avery Stone": {"1": (1.0, 1.0)}})
    (run / "assignment_spec.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        ppp.load_rows(run)

    assert str(run / "assignment_spec.json") in str(excinfo.value)
    # main catches (OSError, ValueError), so the shape guard has to raise one.
    assert ppp.main([str(run)]) == 1


def test_load_rows_rejects_a_problems_entry_that_is_not_an_object(tmp_path: Path) -> None:
    run = _write_run(tmp_path, {"Avery Stone": {"1": (1.0, 1.0)}})
    path = _rewrite_grades(run, problems={"1": 5})

    with pytest.raises(ValueError) as excinfo:
        ppp.load_rows(run)

    message = str(excinfo.value)
    assert str(path) in message
    assert "problem 1" in message


def test_load_rows_rejects_a_children_value_that_is_not_a_list(tmp_path: Path) -> None:
    """A string ``children`` would otherwise be walked one character at a time."""
    run = _write_run(tmp_path, {"Avery Stone": {"1": (1.0, 1.0)}})
    spec = json.loads((run / "assignment_spec.json").read_text(encoding="utf-8"))
    spec["problems"][0]["children"] = "12a"
    (run / "assignment_spec.json").write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        ppp.load_rows(run)

    message = str(excinfo.value)
    assert str(run / "assignment_spec.json") in message
    assert "children of problem 1" in message


def test_load_rows_names_the_file_holding_unparseable_json(tmp_path: Path) -> None:
    """`json` reports line and column only, which is useless across a roster."""
    run = _write_run(tmp_path, {"Avery Stone": {"1": (1.0, 1.0)}})
    path = run / "students" / "Avery_Stone" / "grades.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        ppp.load_rows(run)

    assert str(path) in str(excinfo.value)


def test_main_prints_a_table_and_writes_a_csv(tmp_path: Path, capsys) -> None:
    run = _write_run(
        tmp_path,
        {
            "Avery Stone": {"1": (1.0, 1.0), "2": (0.0, 1.0), "12a": (0.5, 1.0), "16": (2.0, 2.0)},
            "Blake Rivera": {"1": (1.0, 1.0), "2": (1.0, 1.0), "12a": (1.0, 1.0), "16": (0.0, 2.0)},
        },
    )

    assert ppp.main([str(run)]) == 0

    out = capsys.readouterr().out
    assert "Avery Stone" in out
    assert "p1" in out and "p3" in out

    with (run / "points_per_page.csv").open(newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))
    by_id = {row["student_id"]: row for row in written}
    assert by_id["Avery Stone"]["page_1_awarded"] == "1.0"
    assert by_id["Avery Stone"]["page_1_possible"] == "2.0"
    assert by_id["Avery Stone"]["page_1_percent"] == "50.0"
    assert by_id["Blake Rivera"]["page_3_percent"] == "0.0"
    assert by_id["Avery Stone"]["total_awarded"] == "3.5"


def test_main_reports_a_reconciliation_failure(tmp_path: Path, capsys) -> None:
    run = _write_run(tmp_path, {"Avery Stone": {"1": (1.0, 1.0)}})
    path = run / "students" / "Avery_Stone" / "grades.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["total_awarded"] = 42.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    # 3, not 2: argparse spends 2 on usage errors before `main` returns, so a
    # caller branching on the status could not otherwise tell the two apart.
    assert ppp.main([str(run)]) == 3

    assert "42" in capsys.readouterr().err


def test_main_rejects_a_bad_flag_with_the_usage_status(tmp_path: Path) -> None:
    """argparse exits 2 itself, which is why reconciliation failure claims 3."""
    run = _write_run(tmp_path, {"Avery Stone": {"1": (1.0, 1.0)}})

    with pytest.raises(SystemExit) as excinfo:
        ppp.main([str(run), "--nope"])

    assert excinfo.value.code == 2


def test_main_rejects_a_directory_that_is_not_a_run(tmp_path: Path, capsys) -> None:
    assert ppp.main([str(tmp_path)]) == 1

    assert "assignment_spec.json" in capsys.readouterr().err


def test_main_honours_an_explicit_csv_destination(tmp_path: Path) -> None:
    run = _write_run(tmp_path, {"Avery Stone": {"1": (1.0, 1.0)}})
    destination = tmp_path / "elsewhere" / "pages.csv"

    assert ppp.main([str(run), "--csv", str(destination)]) == 0

    assert destination.exists()
    assert not (run / "points_per_page.csv").exists()


def test_main_reports_a_csv_destination_it_cannot_create(tmp_path: Path, capsys) -> None:
    """An unwritable destination is an error message, not a traceback.

    The parent is an existing *file*, so `mkdir` fails on every platform;
    relying on directory permissions would not survive a root CI runner.
    """
    run = _write_run(tmp_path, {"Avery Stone": {"1": (1.0, 1.0)}})
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    assert ppp.main([str(run), "--csv", str(blocker / "pages.csv")]) == 1

    captured = capsys.readouterr()
    assert "error:" in captured.err
    # The table is rendered before the write is attempted, so a failed write
    # still leaves the user the report they asked for.
    assert "Avery Stone" in captured.out
    assert "wrote" not in captured.out


def test_main_still_reports_a_bad_reconciliation_when_the_csv_cannot_be_written(
    tmp_path: Path, capsys
) -> None:
    """A failed write must not suppress the proof that the table is wrong.

    This is the compound case: the run does not reconcile *and* the CSV
    destination cannot be created. Printing the untrustworthy table while the
    banner and the per-student warning were swallowed by an unrelated I/O
    failure is the one outcome this script must never produce.
    """
    run = _write_run(tmp_path, {"Avery Stone": {"1": (1.0, 1.0)}})
    _rewrite_grades(run, total_awarded=42.0)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    # 1, not 3: the unwritable path is the actionable failure, and the banner
    # reaches stderr either way.
    assert ppp.main([str(run), "--csv", str(blocker / "pages.csv")]) == 1

    captured = capsys.readouterr()
    assert "the figures above are not trustworthy" in captured.err
    assert "do not match the recorded 42.0000" in captured.err
    assert "Avery Stone" in captured.out
    assert "wrote" not in captured.out


HOMEWORK_2_PAGES = {
    "1": 1, "2": 1, "3": 1, "4": 1, "5": 1,
    "6": 2, "7": 2, "8": 2, "9": 2,
    "10": 3, "11": 3, "12a": 3, "12b": 3,
    "13": 4, "14a": 4, "14b": 4,
    "15a": 5, "15b": 5, "15c": 5,
    "16a.i": 6, "16a.ii": 6, "16b": 6, "16c": 6,
    "16d": 7, "16e": 7,
}

# The nine problems the mapper located no region for in the real run.
REGIONLESS = ("15a", "15b", "15c", "16a.i", "16a.ii", "16b", "16c", "16d", "16e")


def _homework_2_spec() -> dict:
    def leaf(problem_id: str) -> dict:
        return {"id": problem_id, "pages": [HOMEWORK_2_PAGES[problem_id]], "children": []}

    return {
        "title": "One-Dimensional Kinematics",
        "n_pages": 7,
        "problems": [
            leaf("1"),
            leaf("2"),
            leaf("3"),
            {"id": "4-5", "pages": [1], "children": [leaf("4"), leaf("5")]},
            {"id": "6-7", "pages": [2], "children": [leaf("6"), leaf("7")]},
            leaf("8"),
            leaf("9"),
            leaf("10"),
            leaf("11"),
            {"id": "12", "pages": [3], "children": [leaf("12a"), leaf("12b")]},
            leaf("13"),
            {"id": "14", "pages": [4], "children": [leaf("14a"), leaf("14b")]},
            {"id": "15", "pages": [5], "children": [leaf("15a"), leaf("15b"), leaf("15c")]},
            {
                "id": "16",
                "pages": [6, 7],
                "children": [
                    {"id": "16a", "pages": [6], "children": [leaf("16a.i"), leaf("16a.ii")]},
                    leaf("16b"),
                    leaf("16c"),
                    leaf("16d"),
                    leaf("16e"),
                ],
            },
        ],
    }


def _homework_2_run(root: Path, awarded_by_student: dict[str, dict[str, float]]) -> Path:
    run = root / "run"
    (run / "students").mkdir(parents=True)
    (run / "assignment_spec.json").write_text(json.dumps(_homework_2_spec()), encoding="utf-8")

    summary_rows = []
    for student_id, awarded in awarded_by_student.items():
        problems = {pid: (awarded.get(pid, 0.0), 1.0) for pid in HOMEWORK_2_PAGES}
        payload = _grades(problems)
        payload["student_id"] = student_id
        directory = run / "students" / student_id.replace(" ", "_")
        directory.mkdir()
        (directory / "grades.json").write_text(json.dumps(payload), encoding="utf-8")
        summary_rows.append(
            {
                "student_id": student_id,
                "total_awarded": payload["total_awarded"],
                "total_possible": payload["total_possible"],
            }
        )

    with (run / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["student_id", "total_awarded", "total_possible"])
        writer.writeheader()
        writer.writerows(summary_rows)
    return run


def test_an_incomplete_row_is_flagged_and_a_complete_one_is_not() -> None:
    complete = ppp.accumulate(_grades({"1": (1.0, 1.0)}, student_id="Whole"), {"1": [1]})
    partial = ppp.accumulate(
        _grades({"1": (1.0, 2.0)}, student_id="Partial", total_awarded=None,
                processed_awarded=1.0, score_complete=False),
        {"1": [1]},
    )
    labels = {row.student_id: row.student_id for row in (complete, partial)}

    table = ppp.render_table([complete, partial], [1], labels)

    lines = {line.split()[0]: line for line in table.splitlines() if line.split()}
    assert "!" in lines["Partial"]
    assert "!" not in lines["Whole"]


def test_a_row_that_failed_to_reconcile_is_flagged() -> None:
    """The `!` marks either failure; only the incomplete-score half was covered.

    Deleting `and row.reconciled` from the flag left every other test green,
    yet that flag is what keeps a failed CSV write survivable: the table itself
    must say which rows are not to be trusted.
    """
    row = ppp.accumulate(_grades({"1": (1.0, 1.0)}, total_awarded=99.0), {"1": [1]})
    ppp.reconcile(row)
    labels = {row.student_id: row.student_id}

    assert row.score_complete is True
    assert row.reconciled is False
    assert "!" in ppp.render_table([row], [1], labels)


def test_the_horizontal_rule_matches_the_width_of_the_header() -> None:
    """A rule counting a separator after the final column overhangs by two."""
    row = ppp.accumulate(_grades({"1": (1.0, 1.0)}), {"1": [1]})
    labels = {row.student_id: row.student_id}

    lines = ppp.render_table([row], [1], labels).splitlines()

    assert set(lines[1]) == {"-"}
    assert len(lines[1]) == len(lines[0])


def test_the_unknown_column_appears_only_when_points_land_there() -> None:
    clean = ppp.accumulate(_grades({"1": (1.0, 1.0)}), {"1": [1]})
    labels = {clean.student_id: clean.student_id}
    assert "unknown" not in ppp.render_table([clean], [1], labels)

    stray = ppp.accumulate(_grades({"1": (1.0, 1.0), "99": (0.5, 2.0)}), {"1": [1]})
    labels = {stray.student_id: stray.student_id}
    assert "unknown" in ppp.render_table([stray], [1], labels)


def test_display_labels_truncate_a_long_lone_identifier() -> None:
    long_id = "PHYS 101 - Homework 2 (1D Kinematics) - Avery Stone"
    label = ppp.display_labels([long_id])[long_id]

    assert label.startswith("…")
    assert label.endswith("Avery Stone")
    assert len(label) == 40


def test_the_class_row_pools_rather_than_averaging_percentages() -> None:
    """A mean-of-per-student-percentages implementation must fail this."""
    rows = [
        ppp.accumulate(_grades({"1": (1.0, 1.0)}), {"1": [1]}),
        ppp.accumulate(_grades({"1": (0.0, 9.0)}), {"1": [1]}),
    ]
    labels = {row.student_id: row.student_id for row in rows}

    table = ppp.render_table(rows, [1], labels)

    # Pooled: 1 awarded of 10 possible = 10.0%. Mean of percentages would be
    # (100% + 0%) / 2 = 50.0%.
    assert "10.0%" in table
    assert "50.0%" not in table


def test_a_page_with_nothing_possible_renders_an_empty_percentage() -> None:
    rows = [ppp.accumulate(_grades({"1": (0.0, 0.0)}), {"1": [1]})]
    labels = {rows[0].student_id: rows[0].student_id}

    table = ppp.render_table(rows, [1], labels)

    assert "%" not in table.splitlines()[-1].replace("CLASS", "")


def test_the_csv_neutralizes_an_untrusted_student_id(tmp_path: Path) -> None:
    row = ppp.accumulate(_grades({"1": (1.0, 1.0)}), {"1": [1]})
    row.student_id = "=cmd()"
    destination = tmp_path / "out.csv"

    ppp.write_csv(destination, [row], [1])

    with destination.open(newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))
    assert written[0]["student_id"] == "'=cmd()"


def test_a_mixed_row_preserves_the_total_across_page_and_unknown_buckets() -> None:
    """The core invariant, with both buckets populated at once."""
    grades = _grades({"1": (1.0, 2.0), "99": (0.5, 3.0)})
    row = ppp.accumulate(grades, {"1": [1]})

    assert row.per_page[1].awarded == 1.0
    assert row.unknown.awarded == 0.5
    assert sum(page.awarded for page in row.per_page.values()) + row.unknown.awarded == row.total_awarded
    assert sum(page.possible for page in row.per_page.values()) + row.unknown.possible == row.total_possible
    assert len(row.warnings) == 1


def test_homework_2_shape_reconciles_against_the_recorded_summary(tmp_path: Path) -> None:
    everything = dict.fromkeys(HOMEWORK_2_PAGES, 1.0)
    missing_tail = {pid: 1.0 for pid in HOMEWORK_2_PAGES if pid not in REGIONLESS}
    run = _homework_2_run(tmp_path, {"Avery Stone": everything, "Blake Rivera": missing_tail})

    spec = json.loads((run / "assignment_spec.json").read_text(encoding="utf-8"))
    assert len(ppp.flatten_leaves(spec["problems"])) == 25

    rows, pages = ppp.load_rows(run)
    assert pages == [1, 2, 3, 4, 5, 6, 7]

    by_id = {row.student_id: row for row in rows}
    sparse = by_id["Blake Rivera"]
    assert sparse.total_awarded == 16.0
    assert (sparse.per_page[1].awarded, sparse.per_page[1].possible) == (5.0, 5.0)
    assert (sparse.per_page[4].awarded, sparse.per_page[4].possible) == (3.0, 3.0)
    assert (sparse.per_page[5].awarded, sparse.per_page[5].possible) == (0.0, 3.0)
    assert (sparse.per_page[6].awarded, sparse.per_page[6].possible) == (0.0, 4.0)
    assert (sparse.per_page[7].awarded, sparse.per_page[7].possible) == (0.0, 2.0)
    assert sparse.unknown.possible == 0.0

    with (run / "summary.csv").open(newline="", encoding="utf-8") as handle:
        recorded = {row["student_id"]: float(row["total_awarded"]) for row in csv.DictReader(handle)}

    for row in rows:
        page_sum = sum(page.awarded for page in row.per_page.values()) + row.unknown.awarded
        assert abs(page_sum - recorded[row.student_id]) < ppp.TOLERANCE
        assert row.reconciled
