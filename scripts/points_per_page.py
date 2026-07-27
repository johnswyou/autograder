#!/usr/bin/env python3
"""Report the points each student obtained on each page of a blank assignment.

Reads a completed ``autograder grade --out`` directory. Standalone by design:
it imports only the standard library, so it runs without the project
installed.
"""

from __future__ import annotations

import argparse
import csv
import json
import os.path
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Points are sums of rubric values such as 0.4 and 0.75, so the comparison
# against the recorded total must tolerate binary floating-point drift.
TOLERANCE = 1e-6


def flatten_leaves(problems: object) -> dict[str, list[int]]:
    """Map every terminal problem id to the assignment pages it is printed on.

    A problem is terminal when it has no children *and* is not a container.
    This mirrors ``Problem.is_leaf`` in the pipeline, which is what decides
    which problems get graded, so only these ids can appear in ``grades.json``.
    A childless container is possible because the pipeline forces
    ``type="container"`` onto any node that has children but never clears it
    from a node that has none; such a node is not gradable and carries no
    points to attribute.

    Pages are returned in the order listed, with non-integer entries dropped.
    A malformed tree raises ``ValueError``: a ``children`` holding a string
    would otherwise be walked one character at a time and surface as an
    ``AttributeError`` traceback.
    """
    leaves: dict[str, list[int]] = {}

    def walk(nodes: object, where: str) -> None:
        if not isinstance(nodes, list):
            raise ValueError(f"{where} must be a list, found {type(nodes).__name__}")
        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError(f"{where} must hold objects, found {type(node).__name__}")
            children = node.get("children") or []
            if children:
                walk(children, f"the children of problem {node.get('id')}")
                continue
            if node.get("type") == "container":
                continue
            raw_pages = node.get("pages") or []
            # bool is a subclass of int, so `true` would otherwise become page 1.
            pages = [page for page in raw_pages if isinstance(page, int) and not isinstance(page, bool)]
            leaves[str(node.get("id"))] = pages

    walk(problems, "problems")
    return leaves


@dataclass
class Totals:
    """Awarded and possible points accumulated for one page."""

    awarded: float = 0.0
    possible: float = 0.0

    def add(self, awarded: float, possible: float) -> None:
        self.awarded += awarded
        self.possible += possible


@dataclass
class StudentRow:
    """One student's points, split across the pages of the blank assignment."""

    student_id: str
    per_page: dict[int, Totals] = field(default_factory=dict)
    unknown: Totals = field(default_factory=Totals)
    total_awarded: float = 0.0
    total_possible: float = 0.0
    score_complete: bool = True
    reconciled: bool = True
    warnings: list[str] = field(default_factory=list)


def _number(value: object) -> float:
    """Read a JSON number defensively; absent or null becomes zero."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def accumulate(grades: dict, leaf_pages: dict[str, list[int]]) -> StudentRow:
    """Split one student's graded problems across assignment pages.

    Points that cannot be attributed to a page land in ``unknown`` rather than
    being dropped, so the row always sums back to the recorded score.

    A ``problems`` entry that is not an object raises ``ValueError`` rather
    than escaping as an ``AttributeError`` from the first ``.get``.
    """
    # ``total_awarded`` is null exactly when ``score_complete`` is false; the
    # points earned on the problems that did grade live in ``processed_awarded``
    # instead. Reading that null as zero would leave the page buckets holding
    # real points while the row claimed a total of 0. ``total_possible`` needs no
    # such fallback: it already spans every problem, graded or not.
    recorded_awarded = grades.get("total_awarded")
    if recorded_awarded is None:
        recorded_awarded = grades.get("processed_awarded")
    row = StudentRow(
        student_id=str(grades.get("student_id") or ""),
        total_awarded=_number(recorded_awarded),
        total_possible=_number(grades.get("total_possible")),
        score_complete=bool(grades.get("score_complete", True)),
    )
    problems = grades.get("problems") or {}
    if not isinstance(problems, dict):
        raise ValueError(f"problems must be an object, found {type(problems).__name__}")
    for problem_id, problem in problems.items():
        if not isinstance(problem, dict):
            raise ValueError(f"problem {problem_id} must be an object, found {type(problem).__name__}")
        awarded = _number(problem.get("awarded"))
        possible = _number(problem.get("possible"))
        if problem_id not in leaf_pages:
            row.unknown.add(awarded, possible)
            row.warnings.append(
                f"problem {problem_id} was graded but is absent from assignment_spec.json"
            )
            continue
        pages = leaf_pages[problem_id]
        if not pages:
            row.unknown.add(awarded, possible)
            row.warnings.append(
                f"problem {problem_id} has no page recorded in assignment_spec.json"
            )
            continue
        row.per_page.setdefault(pages[0], Totals()).add(awarded, possible)
    return row


def reconcile(row: StudentRow) -> None:
    """Verify the page buckets sum back to the score the run already recorded.

    A mismatch means the attribution is wrong. Record it on the row so the
    caller can report the discrepancy instead of presenting the numbers as
    trustworthy.

    ``reconciled`` is assigned unconditionally rather than only cleared, so a
    row whose totals were corrected between calls re-validates instead of
    keeping a stale verdict.
    """
    awarded = sum(page.awarded for page in row.per_page.values()) + row.unknown.awarded
    possible = sum(page.possible for page in row.per_page.values()) + row.unknown.possible
    row.reconciled = abs(awarded - row.total_awarded) <= TOLERANCE and abs(possible - row.total_possible) <= TOLERANCE
    if not row.reconciled:
        # Fixed 4-decimal precision, not `:g`: the recorded points are already
        # rounded to 4dp upstream, and `:g`'s 6 significant digits would render
        # two genuinely different totals identically, making the warning read as
        # its own counterexample ("1/1 do not match the recorded 1/1").
        message = (
            f"per-page totals {awarded:.4f}/{possible:.4f} do not match the recorded "
            f"{row.total_awarded:.4f}/{row.total_possible:.4f}"
        )
        # Appending only an unrecorded discrepancy keeps repeat calls idempotent.
        # `warnings` is shared with ``accumulate``, so this must add rather than
        # replace, and it cannot simply clear the list to drop a stale verdict.
        if message not in row.warnings:
            row.warnings.append(message)


def percent(awarded: float, possible: float) -> float | None:
    """Return a percentage, or None when no points were possible."""
    if possible <= 0:
        return None
    return 100.0 * awarded / possible


def csv_text(value: object) -> str:
    """Neutralize spreadsheet formulas in text-valued CSV cells.

    This mirrors ``autograder.report.csv_text``. It is duplicated rather than
    imported to keep this script runnable without the project installed;
    ``tests/test_points_per_page.py`` asserts the two agree.
    """
    text = str(value)
    formula_capable = False
    for character in text:
        if character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F:
            continue
        formula_capable = character in "=+-@"
        break
    text = text.replace("\x00", r"\x00")
    return "'" + text if formula_capable else text


def page_columns(spec: dict, leaf_pages: dict[str, list[int]]) -> list[int]:
    """List every page to show, including pages that carry no points."""
    pages = {pages_for[0] for pages_for in leaf_pages.values() if pages_for}
    n_pages = spec.get("n_pages")
    if isinstance(n_pages, int) and not isinstance(n_pages, bool) and n_pages > 0:
        pages |= set(range(1, n_pages + 1))
    return sorted(pages)


def display_labels(student_ids: list[str], width: int = 40) -> dict[str, str]:
    """Shorten identifiers for display by dropping the prefix they all share.

    A prefix common to every student carries no information. The CSV always
    keeps the full identifier.
    """
    prefix = os.path.commonprefix(sorted(student_ids)) if len(student_ids) > 1 else ""
    # commonprefix is character-level, so trim back to the last field separator:
    # ["Avery Stone", "Ada Nolan"] share "A", and stripping that yields "very"/"da".
    cut = max((prefix.rfind(separator) for separator in " -_/:"), default=-1)
    prefix = prefix[: cut + 1] if cut >= 0 else ""
    labels: dict[str, str] = {}
    for student_id in student_ids:
        label = student_id[len(prefix) :] or student_id
        if len(label) > width:
            label = "…" + label[-(width - 1) :]
        labels[student_id] = label
    return labels


def _load_object(path: Path) -> dict:
    """Parse one JSON object, naming the file in every failure it can raise.

    ``json`` reports a syntax error without the filename, which is unusable
    across a roster of directories, and a top level that parses but is not an
    object would surface as an ``AttributeError`` traceback from the first
    ``.get`` rather than as a message about the file.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object, found {type(payload).__name__}")
    return payload


def load_rows(run_dir: Path) -> tuple[list[StudentRow], list[int]]:
    """Read a run directory into one reconciled row per student."""
    spec_path = run_dir / "assignment_spec.json"
    if not spec_path.is_file():
        raise FileNotFoundError(f"no assignment_spec.json in {run_dir}")
    spec = _load_object(spec_path)
    try:
        leaf_pages = flatten_leaves(spec.get("problems") or [])
    except ValueError as error:
        raise ValueError(f"{spec_path}: {error}") from error

    rows: list[StudentRow] = []
    students_dir = run_dir / "students"
    for grades_path in sorted(students_dir.glob("*/grades.json")):
        grades = _load_object(grades_path)
        try:
            row = accumulate(grades, leaf_pages)
        except ValueError as error:
            raise ValueError(f"{grades_path}: {error}") from error
        if not row.student_id:
            row.student_id = grades_path.parent.name
        reconcile(row)
        rows.append(row)
    if not rows:
        raise FileNotFoundError(f"no students/*/grades.json under {run_dir}")

    rows.sort(key=lambda row: row.student_id)
    return rows, page_columns(spec, leaf_pages)


def _cell(totals: Totals) -> str:
    return f"{totals.awarded:g}/{totals.possible:g}"


def _percent_text(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}%"


def render_table(rows: list[StudentRow], pages: list[int], labels: dict[str, str]) -> str:
    """Render the students-by-pages table, closing with pooled class percentages."""
    show_unknown = any(row.unknown.possible or row.unknown.awarded for row in rows)
    headers = ["student", *[f"p{page}" for page in pages]]
    if show_unknown:
        headers.append("unknown")
    headers.append("total")

    body: list[list[str]] = []
    for row in rows:
        cells = [labels[row.student_id]]
        cells += [_cell(row.per_page.get(page, Totals())) for page in pages]
        if show_unknown:
            cells.append(_cell(row.unknown))
        total = f"{row.total_awarded:g}/{row.total_possible:g}"
        flags = "" if row.score_complete and row.reconciled else "  !"
        cells.append(total + flags)
        body.append(cells)

    # Pool the class rather than averaging per-student percentages, so an
    # incomplete run with a smaller possible total cannot distort the figure.
    footer = ["CLASS"]
    for page in pages:
        awarded = sum(row.per_page.get(page, Totals()).awarded for row in rows)
        possible = sum(row.per_page.get(page, Totals()).possible for row in rows)
        footer.append(_percent_text(percent(awarded, possible)))
    if show_unknown:
        footer.append(
            _percent_text(
                percent(
                    sum(row.unknown.awarded for row in rows),
                    sum(row.unknown.possible for row in rows),
                )
            )
        )
    footer.append(
        _percent_text(
            percent(
                sum(row.total_awarded for row in rows),
                sum(row.total_possible for row in rows),
            )
        )
    )

    table = [headers, *body, footer]
    widths = [max(len(cells[column]) for cells in table) for column in range(len(headers))]
    lines = []
    for index, cells in enumerate(table):
        line = "  ".join(
            cell.ljust(widths[column]) if column == 0 else cell.rjust(widths[column])
            for column, cell in enumerate(cells)
        )
        lines.append(line.rstrip())
        if index in (0, len(table) - 2):
            # The columns are joined by a two-space separator, so the rule spans
            # the widths plus one separator *between* each pair, not one after
            # every column: the latter overhangs the line by two characters.
            lines.append("-" * (sum(widths) + 2 * (len(widths) - 1)))
    return "\n".join(lines)


def write_csv(path: Path, rows: list[StudentRow], pages: list[int]) -> None:
    """Write the wide per-page CSV, neutralizing untrusted text cells."""
    header = ["student_id"]
    for page in pages:
        header += [f"page_{page}_awarded", f"page_{page}_possible", f"page_{page}_percent"]
    header += [
        "unknown_awarded",
        "unknown_possible",
        "total_awarded",
        "total_possible",
        "percent",
        "score_complete",
        "reconciled",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            record: list[object] = [csv_text(row.student_id)]
            for page in pages:
                totals = row.per_page.get(page, Totals())
                share = percent(totals.awarded, totals.possible)
                record += [
                    round(totals.awarded, 4),
                    round(totals.possible, 4),
                    "" if share is None else round(share, 2),
                ]
            overall = percent(row.total_awarded, row.total_possible)
            record += [
                round(row.unknown.awarded, 4),
                round(row.unknown.possible, 4),
                round(row.total_awarded, 4),
                round(row.total_possible, 4),
                "" if overall is None else round(overall, 2),
                row.score_complete,
                row.reconciled,
            ]
            writer.writerow(record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report the points each student obtained on each page of the blank "
            "assignment, from a completed `autograder grade` output directory."
        ),
        epilog=(
            "exit status: 0 success; 1 the run directory could not be read or the "
            "CSV could not be written; 2 usage error; 3 at least one student's "
            "totals failed to reconcile."
        ),
    )
    parser.add_argument(
        "run_dir",
        type=Path,
        help="the directory passed to `autograder grade --out`",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="where to write the CSV (default: <run_dir>/points_per_page.csv)",
    )
    args = parser.parse_args(argv)

    try:
        rows, pages = load_rows(args.run_dir)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    labels = display_labels([row.student_id for row in rows])
    print(render_table(rows, pages, labels))

    # The warnings and the banner print before the CSV is attempted, so that a
    # run which both fails to reconcile *and* cannot write its CSV still shows
    # the evidence that the table above cannot be trusted. Presenting figures
    # this script has already proved inconsistent, with the proof suppressed by
    # an unrelated I/O failure, is the one outcome it must never have.
    status = 0
    for row in rows:
        for warning in row.warnings:
            print(f"warning: {row.student_id}: {warning}", file=sys.stderr)
        if not row.reconciled:
            # 3, not 2: argparse owns 2 for usage errors and bypasses main().
            status = 3
    if status:
        print(
            "error: at least one student's page totals do not match the recorded "
            "score; the figures above are not trustworthy",
            file=sys.stderr,
        )

    # The table prints before the CSV is attempted, so a failed write still
    # leaves the user their report on stdout.
    destination = args.csv if args.csv is not None else args.run_dir / "points_per_page.csv"
    try:
        write_csv(destination, rows, pages)
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        # 1 outranks a pending 3: the unwritable path is the actionable failure,
        # and the reconciliation banner has already reached stderr regardless.
        return 1
    print(f"\nwrote {destination}")

    return status


if __name__ == "__main__":
    raise SystemExit(main())
