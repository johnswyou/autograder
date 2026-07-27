"""Create a small, synthetic grading demo with typeset work.

The assignment and submission exercise four mapping cases: an inserted blank
page, an answer continued on an extra page, an omitted answer, and an answer
written under the wrong problem number. The files demonstrate document
ingestion and answer mapping; they do not demonstrate handwriting recognition
or OCR accuracy.

Usage:
    python examples/generate_sample.py [output_directory]
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

W, H = 612, 792  # US Letter, points


def _page(doc, lines: list[tuple[float, float, str, float]]):
    page = doc.new_page(width=W, height=H)
    for x, y, text, size in lines:
        page.insert_text((x, y), text, fontsize=size, fontname="helv")
    return page


def make_assignment(path: Path) -> None:
    doc = pymupdf.open()
    _page(doc, [
        (72, 80, "PHYS 101 — Quiz 4: Kinematics & Vectors", 16),
        (72, 110, "Show all work. Use g = 9.8 m/s^2. Total: 20 points.", 11),
        (72, 160, "Problem 1 (8 points). A ball is dropped from rest from a height of 45 m.", 12),
        (90, 190, "(a) [3 pts] How long does it take to reach the ground?", 11),
        (90, 330, "(b) [3 pts] What is its speed just before impact?", 11),
        (90, 470, "(c) [2 pts] Using your answer to (a), find the distance fallen in the", 11),
        (90, 486, "     final second of the drop.", 11),
        (72, 700, "Page 1 of 2", 9),
    ])
    _page(doc, [
        (72, 80, "Problem 2 (6 points). Vector A has magnitude 5.0 at 30 degrees above the", 12),
        (72, 96, "+x axis; vector B = (3.0, -4.0).", 12),
        (90, 130, "(a) [3 pts] Compute the components of A.", 11),
        (90, 300, "(b) [3 pts] Compute the magnitude of A + B.", 11),
        (72, 470, "Problem 3 (6 points). Multiple choice: a projectile is launched at 45", 12),
        (72, 486, "degrees. Neglecting air resistance, at the top of its arc its acceleration is:", 12),
        (90, 520, "(A) zero    (B) 9.8 m/s^2 downward    (C) 9.8 m/s^2 along velocity", 11),
        (90, 540, "(D) less than 9.8 m/s^2", 11),
        (72, 700, "Page 2 of 2", 9),
    ])
    doc.save(path)
    doc.close()


def make_submission(path: Path) -> None:
    """Synthetic student 'Jordan': inserted blank page, continued work,
    problem 2b skipped, and problem 3 mislabeled as 'Q4'."""
    doc = pymupdf.open()
    # page 1: mirrors assignment page 1; 1c says "continued on last page"
    _page(doc, [
        (72, 80, "PHYS 101 Quiz 4 — Jordan Lee", 14),
        (72, 160, "Problem 1", 12),
        (90, 200, "(a) 45 = (1/2)(9.8)t^2  ->  t^2 = 9.18  ->  t = 3.03 s", 11),
        (90, 340, "(b) v = g t = 9.8 * 3.03 = 29.7 m/s", 11),
        (90, 480, "(c) see appended page at the end ->", 11),
    ])
    # page 2: an inserted, accidentally blank page (shifts everything after)
    _page(doc, [(300, 400, " ", 11)])
    # page 3: assignment page 2 content; 2a answered, 2b blank, MC labeled "Q4"
    _page(doc, [
        (72, 80, "Problem 2", 12),
        (90, 130, "(a) Ax = 5 cos30 = 4.33,  Ay = 5 sin30 = 2.50", 11),
        (90, 300, "(b)", 11),
        (72, 470, "Q4: answer is (B), gravity still acts at the top.", 12),
    ])
    # page 4: appended extra page with 1c worked out
    _page(doc, [
        (72, 80, "Extra work — Problem 1(c) continued:", 12),
        (90, 120, "distance in final second = d(3.03) - d(2.03)", 11),
        (90, 140, "= 45 - (1/2)(9.8)(2.03)^2 = 45 - 20.2 = 24.8 m", 11),
    ])
    doc.save(path)
    doc.close()


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "sample"
    (out / "submissions").mkdir(parents=True, exist_ok=True)
    assignment_path = out / "sample_assignment.pdf"
    submissions_path = out / "submissions"
    make_assignment(assignment_path)
    make_submission(submissions_path / "jordan_lee.pdf")

    print(f"\nCreated a synthetic, typeset demo in {out}")
    print(f"Assignment: {assignment_path}")
    print(f"Submissions: {submissions_path}")
    print(
        "This demo exercises page and answer mapping; it does not test "
        "handwriting recognition or OCR accuracy."
    )
    print("\nGrade the demo with:")
    print(f"  autograder grade --assignment {assignment_path} \\")
    print(f"      --submissions {submissions_path} --out {out / 'run'}")


if __name__ == "__main__":
    main()
