"""Offline tests for the PASS 1 completeness guard.

A model that reads an 11-page scanned exam and submits a spec containing one
of its 24 questions produces a perfectly valid ``AssignmentSpec``; nothing
downstream can tell it apart from a genuine one-problem worksheet. These tests
pin the two signals that can: pages the spec never accounts for, and printed
point values that cannot be reconciled with the printed total.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from autograder.assignment import _completeness_issues, build_spec
from autograder.config import RunConfig
from autograder.ingest import Document
from autograder.llm import SUBMIT_TOOL_NAME
from autograder.models import AssignmentSpec, Problem, ProblemType

from .conftest import make_stub_client, tool_use, turn


def _mc(pid: str, page: int, points: float | None = None) -> Problem:
    return Problem(id=pid, label=f"{pid}.", prompt="pick one", type=ProblemType.multiple_choice,
                   points=points, pages=[page])


def _spec(problems: list[Problem], total: float | None = None) -> AssignmentSpec:
    return AssignmentSpec(title="Practice Test", total_points=total, problems=problems)


# -- page coverage ----------------------------------------------------------


def test_flags_a_spec_that_accounts_for_almost_no_pages():
    """The failure this guard exists for: 1 of 24 questions off an 11-page exam."""
    issues = _completeness_issues(_spec([_mc("1", 1)]), n_pages=11)
    assert len(issues) == 1
    assert "10 of 11" in issues[0]
    assert "2, 3, 4" in issues[0]


def test_accepts_a_spec_that_covers_every_page():
    spec = _spec([_mc(str(i), i) for i in range(1, 12)])
    assert _completeness_issues(spec, n_pages=11) == []


def test_accepts_a_cover_page_and_a_formula_sheet():
    """Uncovered pages are normal in small numbers; only a wholesale gap counts."""
    spec = _spec([_mc(str(i), i) for i in range(2, 10)])
    assert _completeness_issues(spec, n_pages=10) == []


def test_a_single_uncovered_page_is_never_flagged():
    """Two pages, one problem: a real one-problem assignment must survive."""
    assert _completeness_issues(_spec([_mc("1", 1)]), n_pages=2) == []


def test_container_pages_count_as_covered():
    """A container's children carry the pages; the parent may span both."""
    spec = _spec([
        Problem(id="1", label="1.", prompt="stem", type=ProblemType.container, pages=[1, 2, 3, 4],
                children=[_mc("1a", 1), _mc("1b", 4)]),
    ])
    assert _completeness_issues(spec, n_pages=4) == []


def test_text_sources_are_not_page_checked():
    """A markdown/LaTeX source is split on paragraph boundaries at a fixed character
    budget, so its 'pages' are chunk indices — a chunk of pure preamble carrying no
    problem is normal and says nothing about completeness."""
    assert _completeness_issues(_spec([_mc("1", 1)]), n_pages=11, check_pages=False) == []


# -- printed point totals ---------------------------------------------------


def test_flags_printed_leaf_points_that_disagree_with_the_printed_total():
    spec = _spec([_mc("1", 1, 10.0), _mc("2", 2, 10.0)], total=45.0)
    issues = _completeness_issues(spec, n_pages=2)
    assert len(issues) == 1
    assert "20" in issues[0] and "45" in issues[0]


def test_flags_printed_leaf_points_that_exceed_the_printed_total():
    """Over-allocation is impossible however the unprinted leaves fall."""
    spec = _spec([_mc("1", 1, 40.0), _mc("2", 2, None)], total=30.0)
    issues = _completeness_issues(spec, n_pages=2)
    assert len(issues) == 1
    assert "40" in issues[0] and "30" in issues[0]


def test_accepts_an_unitemized_section_below_the_printed_total():
    """AP Physics 1 shape: 20 unpriced MC items plus four FRQs printed as 45."""
    problems = [_mc(str(i), 1 + i // 3) for i in range(1, 21)]
    problems += [
        Problem(id=str(i), label=f"{i}.", prompt="frq", type=ProblemType.free_response,
                points=float(pts), pages=[8 + n])
        for n, (i, pts) in enumerate(zip(range(21, 25), [10, 15, 15, 5], strict=True))
    ]
    spec = _spec(problems, total=45.0)
    assert _completeness_issues(spec, n_pages=11) == []


def test_accepts_printed_points_when_no_total_is_printed():
    spec = _spec([_mc("1", 1, 10.0), _mc("2", 2, 10.0)], total=None)
    assert _completeness_issues(spec, n_pages=2) == []


def test_reports_both_signals_together():
    spec = _spec([_mc("1", 1, 10.0)], total=45.0)
    assert len(_completeness_issues(spec, n_pages=11)) == 2


# -- wiring into build_spec -------------------------------------------------


@pytest.fixture()
def six_page_pdf(tmp_path: Path) -> Path:
    doc = pymupdf.open()
    for n in range(6):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 100), f"Question {n + 1}", fontsize=12)
    path = tmp_path / "exam.pdf"
    doc.save(path)
    doc.close()
    return path


def _submission(problems: list[dict], total: float | None = None) -> dict:
    return {"title": "Practice Test", "total_points": total, "problems": problems}


def _leaf(pid: str, page: int) -> dict:
    return {"id": pid, "label": f"{pid}.", "prompt": "q", "type": "multiple_choice",
            "pages": [page], "children": []}


def test_build_spec_sends_the_complaint_back_and_accepts_the_repair(six_page_pdf: Path,
                                                                   cfg: RunConfig):
    client = make_stub_client([
        turn(tool_use(SUBMIT_TOOL_NAME, _submission([_leaf("1", 1)]), id="first")),
        turn(tool_use(SUBMIT_TOOL_NAME,
                      _submission([_leaf(str(i), i) for i in range(1, 7)]), id="second")),
    ])
    assignment = Document.from_path(six_page_pdf, "assignment")
    try:
        spec = build_spec(client, cfg, assignment, None)
    finally:
        assignment.close()

    assert [p.id for p in spec.problems] == ["1", "2", "3", "4", "5", "6"]
    complaint = client.calls[1].messages[-1]
    assert complaint["role"] == "tool"
    assert complaint["tool_call_id"] == "first"
    assert complaint["content"].startswith("ERROR:")
    assert "5 of 6" in complaint["content"]


def test_build_spec_gives_up_after_repeated_incomplete_submissions(six_page_pdf: Path,
                                                                  cfg: RunConfig):
    incomplete = turn(tool_use(SUBMIT_TOOL_NAME, _submission([_leaf("1", 1)]), id="again"))
    client = make_stub_client([incomplete, incomplete, incomplete])
    assignment = Document.from_path(six_page_pdf, "assignment")
    try:
        with pytest.raises(Exception, match="incomplete"):
            build_spec(client, cfg, assignment, None)
    finally:
        assignment.close()
    assert len(client.calls) == 3


def test_build_spec_does_not_page_check_a_markdown_assignment(tmp_path: Path, cfg: RunConfig):
    source = tmp_path / "homework.md"
    source.write_text("# Homework\n\n" + ("Long preamble paragraph. " * 400)
                      + "\n\n1. Compute 2 + 2.\n")
    assignment = Document.from_path(source, "assignment")
    assert assignment.n_pages > 2, "fixture must chunk into several pseudo-pages"

    client = make_stub_client([
        turn(tool_use(SUBMIT_TOOL_NAME, _submission([_leaf("1", 1)]), id="only")),
    ])
    try:
        spec = build_spec(client, cfg, assignment, None)
    finally:
        assignment.close()

    assert [p.id for p in spec.problems] == ["1"]
    assert len(client.calls) == 1
