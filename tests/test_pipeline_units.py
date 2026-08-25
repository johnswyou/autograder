"""Offline tests for rubric validation, grade finalization, mapping cleanup,
report writers, and CLI parsing."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

import autograder.report as report
from autograder import __version__
from autograder.cli import _check_key, _to_config, build_parser, main
from autograder.config import RunConfig
from autograder.grading import apply_review_thresholds, finalize_grade
from autograder.mapping import _normalize_mapping, mapping_summary
from autograder.models import (
    AssignmentSpec,
    Criterion,
    CriterionScore,
    GradeDraft,
    Issue,
    Problem,
    ProblemLocation,
    ProblemType,
    ProcessingStatus,
    Region,
    Rubric,
    RubricProblem,
    Solution,
    SolutionsManual,
    StudentGrade,
    StudentMapping,
    Transcript,
    WorkStatus,
)
from autograder.report import review_queue_md, save_json, student_report_md, summary_csv, write_manifest
from autograder.rubric import (
    PointAllocationError,
    _normalize_rubric,
    _problem_points,
    complete_rubric,
    revalidate_cached_rubric,
    rubric_markdown,
    validate_rubric,
)
from autograder.solutions import solutions_markdown


def _rubric_for(spec: AssignmentSpec) -> Rubric:
    return Rubric(title="R", total_points=10.0, problems=[
        RubricProblem(problem_id="1a", points=3.0, criteria=[
            Criterion(id="1a.c1", description="setup", points=1.0),
            Criterion(id="1a.c2", description="answer", points=2.0)]),
        RubricProblem(problem_id="1b", points=3.0, criteria=[
            Criterion(id="1b.c1", description="all", points=3.0)]),
        RubricProblem(problem_id="2", points=4.0, criteria=[
            Criterion(id="2.c1", description="choice", points=4.0)]),
    ])


# -- rubric --------------------------------------------------------------------


def test_validate_rubric_complete(small_spec, ):
    issues = validate_rubric(_rubric_for(small_spec), small_spec)
    assert issues == []


def test_validate_rubric_catches_problems(small_spec):
    r = _rubric_for(small_spec)
    r.problems = r.problems[:1]                       # drop 1b and 2
    r.problems[0].criteria[0].points = 5.0            # sum 7 != 3
    r.problems.append(RubricProblem(problem_id="9", points=1.0, criteria=[
        Criterion(id="9.c1", description="ghost", points=1.0)]))
    issues = validate_rubric(r, small_spec)
    msgs = " | ".join(i.message for i in issues)
    assert any(i.level == "error" and "INCOMPLETE" in i.message for i in issues)
    assert "1b" in msgs and "unknown problem '9'" in msgs
    assert "sum to" in msgs


def test_normalize_rubric_rescales(small_spec):
    r = Rubric(problems=[RubricProblem(problem_id="1a", points=99.0, criteria=[
        Criterion(id="1a.c1", description="x", points=2.0),
        Criterion(id="1a.c2", description="y", points=2.0)])])
    _normalize_rubric(r, {"1a": 3.0}, {"1a"})
    rp = r.problems[0]
    assert rp.points == 3.0
    assert abs(sum(c.points for c in rp.criteria) - 3.0) < 1e-9


def test_problem_points_use_explicit_leaf_values():
    spec = AssignmentSpec(
        title="Weighted",
        total_points=10.0,
        problems=[
            Problem(id="1", type=ProblemType.numeric, points=3.0),
            Problem(id="2", type=ProblemType.numeric, points=7.0),
        ],
    )

    assert _problem_points(spec) == {"1": 3.0, "2": 7.0}


def test_problem_points_use_unweighted_mode_when_nothing_is_printed():
    spec = AssignmentSpec(
        title="Unweighted",
        problems=[
            Problem(id="1", type=ProblemType.numeric),
            Problem(id="2", type=ProblemType.numeric),
        ],
    )

    assert _problem_points(spec) == {"1": 1.0, "2": 1.0}


def test_problem_points_split_a_printed_assignment_total():
    """A paper worth 10 with two unpriced questions states the split implicitly.
    This used to raise; see tests/test_derived_weights.py for the full rules."""
    spec = AssignmentSpec(
        title="Ambiguous",
        total_points=10.0,
        problems=[
            Problem(id="1", type=ProblemType.numeric),
            Problem(id="2", type=ProblemType.numeric),
        ],
    )

    assert _problem_points(spec) == {"1": 5.0, "2": 5.0}


def test_complete_teacher_rubric_supplies_missing_leaf_weights():
    spec = AssignmentSpec(
        title="Parent-weighted",
        total_points=10.0,
        problems=[
            Problem(
                id="1",
                type=ProblemType.container,
                points=10.0,
                children=[
                    Problem(id="1a", type=ProblemType.numeric),
                    Problem(id="1b", type=ProblemType.numeric),
                ],
            ),
        ],
    )
    provided = Rubric(problems=[
        RubricProblem(problem_id="1a", points=3.0),
        RubricProblem(problem_id="1b", points=7.0),
    ])

    out, _ = complete_rubric(
        client=None,
        cfg=RunConfig(api_key="x"),
        spec=spec,
        manual=SolutionsManual(),
        rubric=provided,
        steer=None,
        meter=None,
    )

    assert {rp.problem_id: rp.points for rp in out.problems} == {
        "1a": 3.0,
        "1b": 7.0,
    }

    bad = Rubric(problems=[
        RubricProblem(problem_id="1a", points=4.0),
        RubricProblem(problem_id="1b", points=5.0),
    ])
    with pytest.raises(
        PointAllocationError,
        match="parent.*1.*10.*explicit leaf weights",
    ):
        _problem_points(spec, bad)


@pytest.mark.parametrize("path", ["complete", "cached"])
def test_explicit_leaf_weights_reject_duplicate_rubric_entries(path):
    spec = AssignmentSpec(
        title="Explicit",
        total_points=10.0,
        problems=[
            Problem(id="1", type=ProblemType.numeric, points=3.0),
            Problem(id="2", type=ProblemType.numeric, points=7.0),
        ],
    )
    duplicate = Rubric(problems=[
        RubricProblem(problem_id="1", points=3.0),
        RubricProblem(problem_id="1", points=3.0),
        RubricProblem(problem_id="2", points=7.0),
    ])

    with pytest.raises(PointAllocationError, match="exactly one.*1"):
        if path == "complete":
            complete_rubric(
                client=None,
                cfg=RunConfig(api_key="x"),
                spec=spec,
                manual=SolutionsManual(),
                rubric=duplicate,
                steer=None,
                meter=None,
            )
        else:
            revalidate_cached_rubric(duplicate, spec)


def test_complete_rubric_generates_missing_explicit_leaf(monkeypatch):
    spec = AssignmentSpec(
        title="Explicit",
        total_points=10.0,
        problems=[
            Problem(id="1", type=ProblemType.numeric, points=3.0),
            Problem(id="2", type=ProblemType.numeric, points=7.0),
        ],
    )
    provided = Rubric(problems=[RubricProblem(problem_id="1", points=3.0)])

    def generate_missing(*args, only_ids, **kwargs):
        assert only_ids == {"2"}
        return Rubric(problems=[RubricProblem(problem_id="2", points=7.0)])

    monkeypatch.setattr("autograder.rubric.generate_rubric", generate_missing)

    out, issues = complete_rubric(
        client=None,
        cfg=RunConfig(api_key="x"),
        spec=spec,
        manual=SolutionsManual(),
        rubric=provided,
        steer=None,
        meter=None,
    )

    assert {rp.problem_id: rp.points for rp in out.problems} == {"1": 3.0, "2": 7.0}
    assert any("auto-generated rubric entries" in issue.message for issue in issues)


def test_complete_rubric_generates_missing_unweighted_leaf(monkeypatch):
    spec = AssignmentSpec(
        title="Unweighted",
        problems=[
            Problem(id="1", type=ProblemType.numeric),
            Problem(id="2", type=ProblemType.numeric),
        ],
    )
    provided = Rubric(problems=[RubricProblem(problem_id="1", points=1.0)])

    def generate_missing(*args, only_ids, **kwargs):
        assert only_ids == {"2"}
        return Rubric(problems=[RubricProblem(problem_id="2", points=1.0)])

    monkeypatch.setattr("autograder.rubric.generate_rubric", generate_missing)

    out, issues = complete_rubric(
        client=None,
        cfg=RunConfig(api_key="x"),
        spec=spec,
        manual=SolutionsManual(),
        rubric=provided,
        steer=None,
        meter=None,
    )

    assert {rp.problem_id: rp.points for rp in out.problems} == {"1": 1.0, "2": 1.0}
    assert any("auto-generated rubric entries" in issue.message for issue in issues)


def test_complete_rubric_normalizes_provided(small_spec):
    """A provided/parsed rubric must be forced to the printed points: criteria
    rescaled to sum to each problem's total, and empty criteria filled — so the
    per-problem `possible` grading sees always matches the printed points."""
    provided = Rubric(title="Provided", problems=[
        RubricProblem(problem_id="1a", points=3.0, criteria=[            # sums to 5, not 3
            Criterion(id="1a.c1", description="setup", points=2.0),
            Criterion(id="1a.c2", description="answer", points=3.0)]),
        RubricProblem(problem_id="1b", points=3.0, criteria=[]),        # empty -> 0/0
        RubricProblem(problem_id="2", points=4.0, criteria=[
            Criterion(id="2.c1", description="choice", points=4.0)]),
    ])
    # All leaves present -> no missing -> no API client needed.
    out, _ = complete_rubric(client=None, cfg=RunConfig(api_key="x"), spec=small_spec,
                             manual=SolutionsManual(), rubric=provided, steer=None, meter=None)
    sums = {rp.problem_id: sum(c.points for c in rp.criteria) for rp in out.problems}
    assert sums == {"1a": 3.0, "1b": 3.0, "2": 4.0}
    assert all(rp.criteria for rp in out.problems)          # no empty criteria survive
    assert out.total_points == 10.0


# -- grading -------------------------------------------------------------------


def _rp() -> RubricProblem:
    return RubricProblem(problem_id="1a", points=3.0, criteria=[
        Criterion(id="1a.c1", description="setup", points=1.0),
        Criterion(id="1a.c2", description="answer", points=2.0)])


def test_finalize_grade_clamps_and_fills(cfg: RunConfig):
    draft = GradeDraft(criteria=[
        CriterionScore(criterion_id="1a.c1", awarded=5.0, possible=1.0, justification="over"),
        CriterionScore(criterion_id="bogus", awarded=1.0, possible=1.0, justification="?"),
    ], feedback="ok", confidence=0.95)
    g = finalize_grade(draft, _rp(), "1a", WorkStatus.answered,
                       ocr_confidence=0.9, location_note=None, cfg=cfg, solution_verified=True)
    by = {c.criterion_id: c for c in g.criteria}
    assert by["1a.c1"].awarded == 1.0          # clamped to criterion max
    assert by["1a.c2"].awarded == 0.0          # omitted -> 0
    assert "bogus" not in by                   # unknown dropped
    assert g.awarded == 1.0 and g.possible == 3.0
    assert g.needs_review                      # omission forces review


def test_finalize_grade_review_triggers(cfg: RunConfig):
    ok = GradeDraft(criteria=[
        CriterionScore(criterion_id="1a.c1", awarded=1.0, possible=1.0, justification="y"),
        CriterionScore(criterion_id="1a.c2", awarded=2.0, possible=2.0, justification="y"),
    ], feedback="good", confidence=0.95)

    g = finalize_grade(ok.model_copy(deep=True), _rp(), "1a", WorkStatus.answered,
                       0.95, None, cfg, solution_verified=True)
    assert not g.needs_review

    g = finalize_grade(ok.model_copy(deep=True), _rp(), "1a", WorkStatus.answered,
                       0.30, None, cfg, solution_verified=True)
    assert g.needs_review and "OCR" in g.review_reason

    g = finalize_grade(ok.model_copy(deep=True), _rp(), "1a", WorkStatus.answered,
                       0.95, None, cfg, solution_verified=False)
    assert g.needs_review and "unverified" in g.review_reason

    low = ok.model_copy(deep=True)
    low.confidence = 0.2
    g = finalize_grade(low, _rp(), "1a", WorkStatus.answered, 0.95, None, cfg, True)
    assert g.needs_review and "grader confidence" in g.review_reason


def _graded(cfg: RunConfig, *, confidence: float, ocr: float, verified: bool = True):
    draft = GradeDraft(criteria=[
        CriterionScore(criterion_id="1a.c1", awarded=1.0, possible=1.0, justification="y"),
        CriterionScore(criterion_id="1a.c2", awarded=2.0, possible=2.0, justification="y"),
    ], feedback="good", confidence=confidence)
    return finalize_grade(draft, _rp(), "1a", WorkStatus.answered, ocr, None, cfg,
                          solution_verified=verified)


def test_review_thresholds_are_reapplied_rather_than_trusted(cfg: RunConfig):
    """The thresholds no longer bind the output directory, so a saved grade may
    have been written under different ones. Re-reading it must re-derive the
    flag from the current settings — in both directions."""
    strict = RunConfig(api_key="x", review_confidence=0.9)
    lenient = RunConfig(api_key="x", review_confidence=0.1)

    # graded when 0.8 was good enough, then re-read under a stricter threshold
    g = _graded(lenient, confidence=0.8, ocr=0.95)
    assert not g.needs_review
    apply_review_thresholds(g, strict)
    assert g.needs_review and "grader confidence 0.80 < 0.90" in g.review_reason

    # and back again: relaxing the threshold must clear a flag it alone caused
    apply_review_thresholds(g, lenient)
    assert not g.needs_review and g.review_reason is None


def test_reapplying_thresholds_keeps_reasons_that_describe_the_work(cfg: RunConfig):
    """Only the two confidence comparisons may move. An unverified solution is a
    fact about the grade, so no threshold change can clear it."""
    g = _graded(cfg, confidence=0.95, ocr=0.95, verified=False)
    assert g.needs_review and "unverified" in g.review_reason

    apply_review_thresholds(g, RunConfig(api_key="x", review_confidence=0.0,
                                         ocr_review_threshold=0.0))
    assert g.needs_review, "an unverified solution must survive any threshold"
    assert "unverified" in g.review_reason


def test_reapplying_thresholds_is_idempotent(cfg: RunConfig):
    """It runs on every read, so repeated application must not accumulate
    duplicate reasons or drift."""
    g = _graded(cfg, confidence=0.2, ocr=0.2, verified=False)
    once = (g.needs_review, g.review_reason)
    for _ in range(3):
        apply_review_thresholds(g, cfg)
    assert (g.needs_review, g.review_reason) == once


def test_reapplying_thresholds_leaves_a_failed_grade_alone(cfg: RunConfig):
    """A failed grade has no grader confidence to compare. Its recorded reason
    is the failure, and recomputation must not overwrite it."""
    from autograder.grading import _unavailable_grade

    g = _unavailable_grade("1a", WorkStatus.answered, 3.0, "transcription",
                           "agent died", None)
    apply_review_thresholds(g, RunConfig(api_key="x", review_confidence=0.0))
    assert g.needs_review
    assert g.review_reason == "transcription unavailable: agent died"
    assert g.awarded is None and g.processing_status is ProcessingStatus.failed


def test_finalize_grade_transcript_flags_force_review(cfg: RunConfig):
    """An injection attempt spotted by the TRANSCRIBER (not the grader) must
    still land the problem in the review queue — README promises it."""
    ok = GradeDraft(criteria=[
        CriterionScore(criterion_id="1a.c1", awarded=1.0, possible=1.0, justification="y"),
        CriterionScore(criterion_id="1a.c2", awarded=2.0, possible=2.0, justification="y"),
    ], feedback="good", confidence=0.95)
    g = finalize_grade(ok, _rp(), "1a", WorkStatus.answered, 0.95, None, cfg,
                       solution_verified=True, transcript_flags=1)
    assert g.needs_review and "integrity" in g.review_reason


def test_auto_zero_with_unmatched_work_needs_review(small_spec, tiny_pdf, cfg: RunConfig):
    """A mapper's no-work observation always needs human confirmation."""
    from autograder.grading import grade_problem
    from autograder.ingest import Document
    from autograder.models import ProblemLocation, Transcript

    doc = Document.from_path(tiny_pdf, "s1")
    leaf = small_spec.find("1a")
    loc = ProblemLocation(status=WorkStatus.not_found)
    t = Transcript(problem_id="1a")

    g = grade_problem(None, cfg, small_spec, doc, doc, leaf, _rp(), None, loc, t,
                      mapper_flags=0, has_unmatched_work=True)
    assert g.awarded == 0.0
    assert g.needs_review and "unattributed work" in g.review_reason

    g = grade_problem(None, cfg, small_spec, doc, doc, leaf, _rp(), None, loc, t,
                      mapper_flags=0, has_unmatched_work=False)
    assert g.awarded == 0.0
    assert g.processing_status is ProcessingStatus.complete
    assert g.needs_review
    assert "confirm" in (g.review_reason or "")

    blank = ProblemLocation(status=WorkStatus.blank)
    g = grade_problem(None, cfg, small_spec, doc, doc, leaf, _rp(), None, blank, t,
                      mapper_flags=0, has_unmatched_work=True)
    assert g.awarded == 0.0
    assert g.processing_status is ProcessingStatus.complete
    assert not g.needs_review                     # blank is a positive observation
    doc.close()


def test_blank_auto_zero_preserves_mapper_integrity_flags(small_spec, tiny_pdf, cfg: RunConfig):
    """A clean blank remains a zero, but mapping integrity concerns need review."""
    from autograder.grading import grade_problem
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "s1")
    leaf = small_spec.find("1a")
    grade = grade_problem(
        None, cfg, small_spec, doc, doc, leaf, _rp(), None,
        ProblemLocation(status=WorkStatus.blank), Transcript(problem_id="1a"),
        mapper_flags=1,
    )

    assert grade.awarded == 0.0
    assert grade.processing_status is ProcessingStatus.complete
    assert grade.needs_review
    assert "integrity" in (grade.review_reason or "")
    doc.close()


def test_blank_with_a_located_region_scores_zero_and_needs_review(small_spec, tiny_pdf, cfg: RunConfig):
    """A no-work verdict that still points at a region is scorable (zero) but a
    human must confirm it — the mapper described a place worth looking at."""
    from autograder.grading import grade_problem
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "s1")
    leaf = small_spec.find("1a")
    grade = grade_problem(
        None, cfg, small_spec, doc, doc, leaf, _rp(), None,
        ProblemLocation(status=WorkStatus.blank,
                        regions=[Region(page=1, bbox=[0, 0, 50, 50])]),
        Transcript(problem_id="1a"),
    )

    assert grade.awarded == 0.0
    assert grade.processing_status is ProcessingStatus.complete
    assert grade.needs_review
    assert "region" in (grade.review_reason or "")
    doc.close()


# -- mapping -------------------------------------------------------------------


def test_normalize_mapping(small_spec, tiny_pdf):
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "s1")  # 2 pages
    m = StudentMapping(problems={
        "1a": ProblemLocation(status=WorkStatus.answered,
                              regions=[Region(page=1, bbox=[0, 0, 50, 50]),
                                       Region(page=9, bbox=[0, 0, 50, 50])]),  # bad page
        "1b": ProblemLocation(status=WorkStatus.answered, regions=[]),         # claims work, none located
        "zz": ProblemLocation(status=WorkStatus.answered),                     # unknown id
    }, extra_pages=[2, 7])
    m = _normalize_mapping(m, small_spec, doc)
    assert set(m.problems) == {"1a", "1b", "2"}
    assert len(m.problems["1a"].regions) == 1
    assert m.problems["1b"].status is WorkStatus.mapping_error
    assert m.problems["2"].status is WorkStatus.mapping_error
    assert m.extra_pages == [2]
    assert "mapping_error=2" in mapping_summary(m)
    doc.close()


def test_normalize_mapping_keeps_no_work_status_that_also_supplied_regions(small_spec, tiny_pdf):
    """A mapper that reports 'blank' while pointing at the empty answer space is
    describing the same page two ways, not contradicting itself. Escalating that
    to mapping_error discards a correct observation and — because one unscorable
    item withholds the whole student total — costs the student their grade."""
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "s1")  # 2 pages
    mapping = StudentMapping(problems={
        "1a": ProblemLocation(
            status=WorkStatus.blank,
            regions=[Region(page=1, bbox=[0, 0, 50, 50])],
        ),
        "1b": ProblemLocation(
            status=WorkStatus.not_found,
            regions=[Region(page=9, bbox=[0, 0, 50, 50])],  # out of range, dropped
        ),
    })

    normalized = _normalize_mapping(mapping, small_spec, doc)

    for problem_id, status in (("1a", WorkStatus.blank), ("1b", WorkStatus.not_found)):
        location = normalized.problems[problem_id]
        assert location.status is status
        assert "region" in (location.note or "")
    assert len(normalized.problems["1a"].regions) == 1
    doc.close()


def test_mapper_is_not_given_blank_assignment_coordinates(small_spec, tiny_pdf, cfg: RunConfig):
    """`answer_region` bboxes are percentages measured on the BLANK assignment.
    Submissions are frequently exports that inset that page into a corner, so the
    percentages do not transfer; handing them to the mapper anchors it on the
    wrong part of the student's page. It must measure on the submission itself."""
    from autograder.ingest import Document
    from autograder.mapping import map_student

    from .conftest import make_stub_client, tool_use

    spec = small_spec.model_copy(deep=True)
    spec.find("1a").answer_region = Region(page=1, bbox=[10, 17, 45, 27])
    doc = Document.from_path(tiny_pdf, "s1")
    client = make_stub_client([[tool_use(
        "submit_result",
        {"problems": {pid: {"status": "blank"} for pid in spec.leaf_ids()}},
    )]])

    map_student(client, cfg, spec, doc, doc)

    sent = "".join(b["text"] for b in client.calls[0].messages[1]["content"]
                   if isinstance(b, dict) and b.get("type") == "text")
    assert "1a" in sent and "1b" in sent          # the inventory itself still reaches the mapper
    assert "answer_region" not in sent
    doc.close()


def test_mapper_prompt_shows_a_well_formed_percent_bbox():
    """Dropping the answer_region hints also removed the mapper's only worked
    example of the coordinate format, and it began emitting out-of-range values.
    The exemplar has to live in the prompt, where no document can distort it."""
    import re

    from autograder.mapping import MAPPER_SYSTEM

    examples = [
        [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", match)]
        for match in re.findall(r"\[[\d.,\s-]+\]", MAPPER_SYSTEM)
    ]
    four_number = [e for e in examples if len(e) == 4]
    assert four_number, "the mapper prompt shows no example bbox"
    assert any(all(0.0 <= v <= 100.0 for v in e) and e[0] < e[2] and e[1] < e[3]
               for e in four_number), "no example bbox is a well-formed percent rectangle"


def test_mapping_error_is_unavailable_without_agent_calls(small_spec, tiny_pdf, cfg: RunConfig):
    from autograder.grading import grade_problem
    from autograder.ingest import Document
    from autograder.ocr import transcribe_problem

    doc = Document.from_path(tiny_pdf, "s1")
    leaf = small_spec.find("1a")
    loc = ProblemLocation(status=WorkStatus.mapping_error)

    transcript = transcribe_problem(None, cfg, small_spec, doc, leaf, loc)
    assert transcript.processing_status is ProcessingStatus.failed
    assert transcript.failure is not None and transcript.failure.stage == "mapping"

    grade = grade_problem(None, cfg, small_spec, doc, doc, leaf, _rp(), None, loc, transcript)
    assert grade.processing_status is ProcessingStatus.failed
    assert grade.failure is not None and grade.failure.stage == "mapping"
    doc.close()


# -- reports -------------------------------------------------------------------


def _grade(small_spec) -> StudentGrade:
    from autograder.models import ProblemGrade
    return StudentGrade(student_id="alice", total_awarded=6.0, total_possible=10.0,
                        problems={
                            "1a": ProblemGrade(problem_id="1a", status=WorkStatus.answered,
                                               awarded=3.0, possible=3.0, feedback="nice",
                                               confidence=0.9),
                            "1b": ProblemGrade(problem_id="1b", status=WorkStatus.blank,
                                               awarded=0.0, possible=3.0, confidence=1.0),
                            "2": ProblemGrade(problem_id="2", status=WorkStatus.answered,
                                              awarded=3.0, possible=4.0, needs_review=True,
                                              review_reason="OCR 0.4", confidence=0.7),
                        })


def test_save_json_uses_atomic_writer(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "artifact.json"
    model = SolutionsManual()
    writes: list[tuple[Path, str]] = []

    monkeypatch.setattr(report, "atomic_write_text", lambda target, text: writes.append((target, text)))

    save_json(path, model)

    assert writes == [(path, model.model_dump_json(indent=2))]


def test_summary_csv_uses_single_atomic_replacement(tmp_path: Path, small_spec, monkeypatch) -> None:
    path = tmp_path / "summary.csv"
    writes: list[tuple[Path, str]] = []

    monkeypatch.setattr(report, "atomic_write_text", lambda target, text: writes.append((target, text)))

    summary_csv(path, small_spec, [_grade(small_spec)])

    assert len(writes) == 1
    assert writes[0][0] == path
    assert "alice" in writes[0][1]


def test_manifest_uses_atomic_writer_and_records_tool_version(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "manifest.json"
    writes: list[tuple[Path, str]] = []

    monkeypatch.setattr(
        report,
        "atomic_write_text",
        lambda target, text: writes.append((target, text)),
    )

    write_manifest(
        path,
        RunConfig(
            api_key="secret-never-persisted",
            reasoning_effort="high",
            zero_data_retention=False,
            allow_data_collection=True,
            provider_sort="throughput",
        ),
        {},
        [],
        {
            "api_calls": 2,
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "reasoning_tokens": 3,
            "cached_prompt_tokens": 4,
            "cache_write_tokens": 1,
            "cost_usd": 0.02,
            "resolved_models": ["vendor/resolved"],
            "providers": ["Provider One"],
        },
        datetime(2026, 7, 24, tzinfo=timezone.utc),
        [],
        "complete",
    )

    assert len(writes) == 1
    assert writes[0][0] == path
    manifest = json.loads(writes[0][1])
    assert manifest["tool"] == "agentic-autograder"
    assert manifest["tool_version"] == __version__
    assert manifest["run_status"] == "complete"
    assert manifest["requested_model"] == "openrouter/auto-beta"
    assert manifest["resolved_models"] == ["vendor/resolved"]
    assert manifest["providers"] == ["Provider One"]
    assert manifest["config"]["reasoning_effort"] == "high"
    assert manifest["config"]["zero_data_retention"] is False
    assert manifest["config"]["allow_data_collection"] is True
    assert manifest["config"]["provider_sort"] == "throughput"
    assert manifest["usage"]["cost_usd"] == 0.02
    serialized = writes[0][1]
    assert "secret-never-persisted" not in serialized
    assert "messages" not in serialized


def test_report_writers(tmp_path: Path, small_spec):
    g = _grade(small_spec)
    md = student_report_md(small_spec, g, StudentMapping(),
                           {"1a": Transcript(problem_id="1a", text="$2+2=4$", confidence=0.9)})
    assert "alice" in md and "6 / 10" in md and "Needs human review" in md and "$2\\+2\\=4$" in md

    csv_path = tmp_path / "summary.csv"
    summary_csv(csv_path, small_spec, [g])
    text = csv_path.read_text()
    assert "alice" in text and "60.0" in text

    n = review_queue_md(tmp_path / "rq.md", small_spec, [g])
    assert n == 1
    assert r"OCR 0\.4" in (tmp_path / "rq.md").read_text()


def test_reports_leave_failed_scores_unavailable(tmp_path: Path, small_spec):
    from autograder.models import ArtifactFailure, ProblemGrade

    grade = StudentGrade(
        student_id="alice",
        total_awarded=None,
        total_possible=7.0,
        processed_awarded=3.0,
        processed_possible=3.0,
        score_complete=False,
        problems={
            "1a": ProblemGrade(problem_id="1a", status=WorkStatus.answered,
                               awarded=3.0, possible=3.0),
            "2": ProblemGrade(
                problem_id="2", status=WorkStatus.answered, awarded=None, possible=4.0,
                processing_status=ProcessingStatus.failed,
                failure=ArtifactFailure(stage="grading", message="service unavailable"),
            ),
        },
    )

    report = student_report_md(small_spec, grade, StudentMapping(), {})
    assert "Final score unavailable" in report
    assert "Processed subtotal: 3 / 3" in report
    assert "unavailable / 4" in report
    assert "grading" in report and "service unavailable" in report

    csv_path = tmp_path / "summary.csv"
    summary_csv(csv_path, small_spec, [grade])
    row = next(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert row["total_awarded"] == ""
    assert row["percent"] == ""
    assert row["2"] == ""
    assert row["run_status"] == "incomplete"
    assert row["failure"] == "grading: service unavailable"

    queue_path = tmp_path / "review_queue.md"
    assert review_queue_md(queue_path, small_spec, [grade]) == 1
    queue = queue_path.read_text()
    assert "grading" in queue and "service unavailable" in queue


def test_summary_includes_whole_student_failure(tmp_path: Path, small_spec):
    from autograder.models import StudentFailure

    failure = StudentFailure(student_id="bob", stage="student", message="PDF unreadable")

    csv_path = tmp_path / "summary.csv"
    summary_csv(csv_path, small_spec, [], [failure])
    row = next(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert row["student_id"] == "bob"
    assert row["total_awarded"] == ""
    assert row["total_possible"] == ""
    assert row["percent"] == ""
    assert row["run_status"] == "failed"
    assert row["failure"] == "student: PDF unreadable"
    assert all(row[problem_id] == "" for problem_id in small_spec.leaf_ids())

    queue_path = tmp_path / "review_queue.md"
    assert review_queue_md(queue_path, small_spec, [], [failure]) == 1
    assert "bob" in queue_path.read_text()


def test_report_escapes_student_markup(tmp_path: Path, small_spec):
    """Verbatim transcripts are untrusted: markup must not escape the <details>
    block or reach an HTML-rendering viewer unescaped."""
    g = _grade(small_spec)
    hostile = "answer</details><script>alert(1)</script>"
    md = student_report_md(small_spec, g, StudentMapping(),
                           {"1a": Transcript(problem_id="1a", text=hostile, confidence=0.9)})
    assert "<script>" not in md and "answer</details>" not in md
    assert "&lt;script&gt;" in md

    # Multi-line review reasons must remain in one escaped review-table cell.
    g.problems["2"].review_reason = "line one\nline two | with pipe"
    review_queue_md(tmp_path / "rq.md", small_spec, [g])
    rq = (tmp_path / "rq.md").read_text()
    row = [ln for ln in rq.splitlines() if ln.startswith("| alice | 2 |")]
    assert row and r"line one line two \| with pipe" in row[0]


def test_report_escapes_all_untrusted_markdown_fields(tmp_path: Path):
    """Report text must remain data even when every report field is hostile."""
    from autograder.models import ArtifactFailure, ProblemGrade

    hostile = (
        "<script>alert(1)</script> </details> "
        "[click](javascript:alert(1)) *emphasis*\nline one\nline two | cell"
    )
    spec = AssignmentSpec(
        title=hostile,
        problems=[
            Problem(id=hostile, label=hostile, points=1.0),
            Problem(id="failed", label=hostile, points=1.0),
        ],
    )
    grade = StudentGrade(
        student_id=hostile,
        total_awarded=None,
        total_possible=2.0,
        processed_awarded=1.0,
        processed_possible=1.0,
        score_complete=False,
        flags=[hostile],
        problems={
            hostile: ProblemGrade(
                problem_id=hostile,
                status=WorkStatus.answered,
                awarded=1.0,
                possible=1.0,
                needs_review=True,
                review_reason=hostile,
                location_note=hostile,
                criteria=[CriterionScore(
                    criterion_id=hostile, awarded=1.0, possible=1.0,
                    justification=hostile,
                )],
                feedback=hostile,
            ),
            "failed": ProblemGrade(
                problem_id="failed",
                status=WorkStatus.answered,
                awarded=None,
                possible=1.0,
                processing_status=ProcessingStatus.failed,
                failure=ArtifactFailure(stage=hostile, message=hostile),
            ),
        },
    )

    rendered = student_report_md(
        spec,
        grade,
        StudentMapping(),
        {hostile: Transcript(problem_id=hostile, text=hostile, confidence=0.9)},
    )

    assert "<script>" not in rendered
    assert rendered.count("</details>") == 1
    assert "[click](javascript:alert(1))" not in rendered
    assert " | cell" not in rendered
    assert r"\| cell" in rendered
    for visible_word in ("script", "click", "emphasis", "line one", "line two", "cell"):
        assert visible_word in rendered

    queue_path = tmp_path / "review_queue.md"
    assert review_queue_md(queue_path, spec, [grade]) == 2
    queue = queue_path.read_text()
    assert "<script>" not in queue
    assert "</details>" not in queue
    assert "[click](javascript:alert(1))" not in queue
    assert " | cell" not in queue
    assert r"\| cell" in queue


def test_review_table_preserves_escaped_pipe(tmp_path: Path):
    from autograder.models import ProblemGrade

    problem_id = "problem | id"
    spec = AssignmentSpec(problems=[Problem(id=problem_id, label="Problem", points=1.0)])
    grade = StudentGrade(
        student_id="student | id",
        total_awarded=1.0,
        total_possible=1.0,
        problems={
            problem_id: ProblemGrade(
                problem_id=problem_id,
                status=WorkStatus.answered,
                awarded=1.0,
                possible=1.0,
                needs_review=True,
                review_reason="with | pipe",
            ),
        },
    )

    review_queue_md(tmp_path / "rq.md", spec, [grade])

    row = (tmp_path / "rq.md").read_text()
    assert r"| student \| id | problem \| id | with \| pipe |" in row


def test_solutions_manual_encodes_all_untrusted_markdown_text():
    """Changing a manual renderer back to direct interpolation must fail here."""
    hostile = "<script>alert(1)</script> </details> [click](javascript:alert(1))"
    title = f"assignment title {hostile}"
    problem_id = f"solution id {hostile}"
    label = f"problem label {hostile}"
    provenance = f"provenance {hostile}"
    assumption = f"assumption {hostile}"
    reasoning = f"reasoning first {hostile}\nreasoning second"
    final_answer = f"final answer first {hostile}\nfinal answer second"
    verifier_notes = f"verifier notes first {hostile}\nverifier notes second"
    spec = AssignmentSpec(
        title=title,
        problems=[Problem(id=problem_id, label=label, points=5.0)],
    )
    manual = SolutionsManual(solutions={
        problem_id: Solution(
            problem_id=problem_id,
            provenance=provenance,
            assumptions=[assumption],
            reasoning=reasoning,
            final_answer=final_answer,
            verifier_notes=verifier_notes,
            verified=True,
            rounds=7,
        ),
    })

    rendered = solutions_markdown(spec, manual)

    assert "<script>" not in rendered
    assert "</details>" not in rendered
    assert "[click](javascript:alert(1))" not in rendered
    assert r"\[click\]\(javascript:alert\(1\)\)" in rendered
    for marker in (
        "assignment title", "solution id", "problem label", "provenance",
        "assumption", "reasoning first", "reasoning second", "final answer first",
        "final answer second", "verifier notes first", "verifier notes second",
    ):
        assert marker in rendered
    assert "\nreasoning second" in rendered
    assert "\nfinal answer second" in rendered
    assert "\n> verifier notes second" in rendered
    assert "rounds: 7" in rendered


def test_rubric_encodes_all_untrusted_markdown_text():
    """Changing a rubric renderer back to direct interpolation must fail here."""
    hostile = "<script>alert(1)</script> </details> [click](javascript:alert(1))"
    title = f"rubric title {hostile}"
    problem_id = f"rubric problem id {hostile}"
    label = f"rubric problem label {hostile}"
    criterion_id = f"criterion id {hostile}"
    description = f"criterion description first {hostile}\ncriterion description second"
    grading_notes = f"grading notes first {hostile}\ngrading notes second"
    spec = AssignmentSpec(
        problems=[Problem(id=problem_id, label=label, points=5.0)],
    )
    rubric = Rubric(
        title=title,
        total_points=5.0,
        problems=[RubricProblem(
            problem_id=problem_id,
            points=5.0,
            criteria=[Criterion(id=criterion_id, description=description, points=5.0)],
            grading_notes=grading_notes,
        )],
    )

    rendered = rubric_markdown(spec, rubric)

    assert "<script>" not in rendered
    assert "</details>" not in rendered
    assert "[click](javascript:alert(1))" not in rendered
    assert r"\[click\]\(javascript:alert\(1\)\)" in rendered
    for marker in (
        "rubric title", "rubric problem id", "rubric problem label", "criterion id",
        "criterion description first", "criterion description second", "grading notes first",
        "grading notes second",
    ):
        assert marker in rendered
    assert "\ncriterion description second" in rendered
    assert "\n> grading notes second" in rendered
    assert "Total: 5 points" in rendered
    assert "— 5 pt" in rendered


def test_csv_text_normalizes_nul_without_hiding_formula_prefix():
    assert report.csv_text("plain\x00text") == r"plain\x00text"
    assert report.csv_text("\x00=failed-message") == r"'\x00=failed-message"


def test_markdown_text_normalizes_nul():
    """A single raw NUL makes a Markdown file read as binary, so a student report
    would stop being viewable or diffable as text."""
    rendered = report.markdown_text("plain\x00text")

    assert "\x00" not in rendered
    assert r"\x00" in rendered.replace("\\\\", "\\")


def test_markdown_text_keeps_quotes_readable():
    """HTML-escaping quotes to a numeric character reference and then
    backslash-escaping '#' produced '&\\#x27;', which renders as the literal text
    '&#x27;' — so every apostrophe in a student-facing report was corrupted."""
    rendered = report.markdown_text("the student's \"best\" work")

    assert "student's" in rendered
    assert '"best"' in rendered
    assert "#x27" not in rendered and "#x22" not in rendered


def test_summary_neutralizes_formula_text_cells(tmp_path: Path):
    from autograder.models import StudentFailure

    grade = StudentGrade(
        student_id=" \x01=student",
        total_awarded=3.0,
        total_possible=4.0,
        flags=["\t+flag"],
    )
    failure = StudentFailure(
        student_id="\u2002@failed-student",
        stage="\x7f-failed-stage",
        message="\x00=failed-message",
    )
    path = tmp_path / "summary.csv"

    summary_csv(path, AssignmentSpec(), [grade], [failure])

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    complete, failed = rows
    assert complete["student_id"].startswith("'")
    assert complete["flags"].startswith("'")
    assert failed["student_id"].startswith("'")
    assert failed["failure"].startswith("'")
    assert "\x00" not in failed["failure"]
    assert r"\x00=failed-message" in failed["failure"]
    assert float(complete["total_awarded"]) == 3.0
    assert float(complete["total_possible"]) == 4.0


# -- cli -----------------------------------------------------------------------


def test_cli_returns_two_for_partial_grade(
    monkeypatch,
    tmp_path: Path,
    tiny_pdf: Path,
    capsys: pytest.CaptureFixture[str],
):
    from autograder.orchestrator import PartialGradeFailure

    incomplete = StudentGrade(
        student_id="alice",
        total_awarded=None,
        total_possible=10.0,
        processed_awarded=3.0,
        processed_possible=3.0,
        score_complete=False,
    )

    def raise_partial(*args, **kwargs):
        raise PartialGradeFailure([incomplete], [])

    monkeypatch.setattr("autograder.cli.Pipeline.run_grade", raise_partial)

    assert main([
        "grade",
        "-a",
        str(tiny_pdf),
        "-o",
        str(tmp_path / "run"),
        "-S",
        str(tiny_pdf),
    ]) == 2
    output = capsys.readouterr().out
    assert (
        "Grading finished with incomplete results: "
        "1 incomplete student record(s), 0 student failure(s)."
    ) in output
    assert "Grading partially failed" not in output


def test_cli_parsing():
    p = build_parser()
    args = p.parse_args([
        "grade", "-a", "hw.pdf", "-o", "out", "-S", "subs/",
        "--rubric-prompt", "weight method", "--reasoning-effort", "high",
        "--provider-sort", "throughput",
        "--allow-data-retention", "--allow-data-collection",
        "--strict-rubric", "--ocr-threshold", "0.7",
    ])
    cfg = _to_config(args)
    assert args.command == "grade" and args.submissions == ["subs/"]
    assert cfg.reasoning_effort == "high"
    assert cfg.provider_sort == "throughput"
    assert cfg.zero_data_retention is False
    assert cfg.allow_data_collection is True
    assert cfg.strict_rubric and cfg.ocr_review_threshold == 0.7

    with pytest.raises(SystemExit):
        p.parse_args(["grade", "-a", "hw.pdf", "-o", "out"])  # missing -S

    args = p.parse_args(["inspect", "-a", "hw.pdf", "-o", "out"])
    assert args.command == "inspect"
    assert _to_config(args).provider_sort is None

    with pytest.raises(SystemExit):
        p.parse_args(["inspect", "-a", "hw.pdf", "-o", "out", "--provider-sort", "cheapest"])


def test_cli_reads_only_openrouter_api_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-key")
    args = build_parser().parse_args(["inspect", "-a", "hw.pdf", "-o", "out"])
    assert _to_config(args).api_key == "openrouter-key"

    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert _to_config(args).api_key is None


@pytest.mark.parametrize("command", ["inspect", "solve", "rubric", "grade"])
def test_cli_help_explains_output_reuse_and_force(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args([command, "--help"])

    assert caught.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Blank assignment file or directory" in help_text
    assert "Directory for generated results" in help_text
    assert "Reuse it only with the same inputs and grading settings" in help_text
    assert "choose a new directory after a change" in help_text
    assert (
        "Request a higher output-token limit for model calls. Values below "
        "the built-in limits have no effect."
    ) in help_text
    assert "OpenRouter model slug" in help_text
    assert "--reasoning-effort" in help_text
    assert "--provider-sort" in help_text
    assert "omit to keep OpenRouter's default balancing" in help_text
    assert "--allow-data-retention" in help_text
    assert "--allow-data-collection" in help_text
    assert "Rebuild this command's results instead of reusing saved results" in help_text
    assert "If inputs or settings changed, choose a new --out directory" in help_text
    assert "--force does not make the old directory reusable" in help_text
    assert "prompt-cache breakpoints" not in help_text


def test_cli_command_summaries_describe_user_outcomes() -> None:
    help_text = " ".join(build_parser().format_help().split())

    assert (
        "Read the assignment and save its problems, parts, and point values"
        in help_text
    )
    assert "Create or check an answer key" in help_text
    assert "Create or check a grading rubric" in help_text
    assert "Grade every submission and write reports" in help_text
    assert "Pass 1" not in help_text
    assert "problem inventory" not in help_text
    assert "generator + independent evaluator" not in help_text


def test_cli_input_help_describes_missing_entry_handling(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["solve", "--help"])
    solve_help = " ".join(capsys.readouterr().out.split())
    assert (
        "Missing answers are generated and independently checked by default."
        in solve_help
    )
    assert (
        "--verify-provided-solutions Independently check supplied answers when "
        "building a solutions manual; saved manuals are reused without "
        "rechecking"
        in solve_help
    )
    assert (
        "--strict-solutions Stop if the provided answer key is incomplete "
        "instead of generating missing answers"
    ) in solve_help

    with pytest.raises(SystemExit):
        build_parser().parse_args(["rubric", "--help"])
    rubric_help = " ".join(capsys.readouterr().out.split())
    assert (
        "Missing problem entries are generated and labeled [auto-generated] "
        "by default."
    ) in rubric_help
    assert (
        "--strict-rubric Stop when any assignment problem lacks a rubric entry "
        "instead of generating the missing entry"
        in rubric_help
    )


def test_cli_grading_help_explains_review_thresholds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["grade", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert (
        "Grading results with model confidence below this value go to the "
        "human review queue"
    ) in help_text
    assert (
        "Transcriptions with model confidence below this value go to the "
        "human review queue"
    ) in help_text
    assert "Grades below this confidence" not in help_text
    assert "OCR confidence" not in help_text


def test_cli_solve_summary_reports_saved_status_without_claiming_review_need(
    monkeypatch,
    tmp_path: Path,
    tiny_pdf: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from autograder.models import Solution

    manual = SolutionsManual(
        solutions={
            "1": Solution(
                problem_id="1",
                final_answer="42",
                verified=True,
                provenance="provided",
            ),
            "2": Solution(
                problem_id="2",
                final_answer="unresolved",
                verified=False,
                provenance="generated",
            ),
        }
    )

    def return_manual(self, path):
        self.assignment.close()
        return manual

    monkeypatch.setattr("autograder.cli.Pipeline.run_solve", return_manual)

    assert main(
        [
            "solve",
            "--assignment",
            str(tiny_pdf),
            "--out",
            str(tmp_path / "run"),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "Solutions manual: 2 entries; 1 marked unverified." in output
    assert "Review " in output
    assert "solutions_manual.md before grading." in output
    assert "needs review" not in output


def test_cli_solve_warns_when_requested_answer_check_could_not_run(
    monkeypatch,
    tmp_path: Path,
    tiny_pdf: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from autograder.models import Solution

    manual = SolutionsManual(
        solutions={
            "1": Solution(
                problem_id="1",
                final_answer="42",
                verified=True,
                provenance="provided",
            ),
        }
    )

    def return_manual(self, path):
        self.issues.append(
            Issue(
                level="warning",
                message=(
                    "could not verify provided solution for 1 "
                    "(evaluator agent failed: API unavailable)"
                ),
            )
        )
        self.assignment.close()
        return manual

    monkeypatch.setattr("autograder.cli.Pipeline.run_solve", return_manual)

    out_dir = tmp_path / "run"
    assert main(
        [
            "solve",
            "--assignment",
            str(tiny_pdf),
            "--solutions",
            str(tiny_pdf),
            "--verify-provided-solutions",
            "--out",
            str(out_dir),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert (
        "Independent checking was unavailable for 1 supplied answer." in output
    )
    assert (
        "This failure does not mark the answer unverified or add dependent "
        "grades to the review queue."
        in output
    )
    assert "Review it manually" in output
    assert "retry with a new --out directory" in output
    assert f"Details: {out_dir / 'run_manifest.json'}" in output


def test_cli_inspect_summary_names_the_saved_assignment_structure(
    monkeypatch,
    tmp_path: Path,
    tiny_pdf: Path,
    small_spec: AssignmentSpec,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def return_spec(self):
        self.assignment.close()
        return small_spec

    monkeypatch.setattr("autograder.cli.Pipeline.run_inspect", return_spec)

    out_dir = tmp_path / "run"
    assert main(
        [
            "inspect",
            "--assignment",
            str(tiny_pdf),
            "--out",
            str(out_dir),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert f"Assignment structure written to {out_dir / 'assignment_spec.json'}" in output
    assert "Spec written" not in output


def test_cli_interruption_explains_how_to_resume(
    monkeypatch,
    tmp_path: Path,
    tiny_pdf: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def interrupt(self):
        self.assignment.close()
        raise KeyboardInterrupt

    monkeypatch.setattr("autograder.cli.Pipeline.run_inspect", interrupt)

    with caplog.at_level(logging.ERROR, logger="autograder"):
        assert main(
            [
                "inspect",
                "--assignment",
                str(tiny_pdf),
                "--out",
                str(tmp_path / "run"),
            ]
        ) == 130

    assert (
        "Interrupted. Run the same command again with the same inputs and --out "
        "directory to resume."
    ) in caplog.text
    assert "cached artifacts" not in caplog.text


def test_cli_missing_key_warning_states_consequence_and_fix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="autograder"):
        _check_key(RunConfig(api_key=None))

    assert (
        "OPENROUTER_API_KEY is not set. Saved results can still be reused, but "
        "the command will stop if it needs to call the model. Set the environment "
        "variable or pass --api-key."
    ) in caplog.text
    assert "cached stages" not in caplog.text


@pytest.mark.parametrize("removed_option", ["--thinking", "--effort", "--no-prompt-caching"])
def test_cli_rejects_anthropic_era_options(removed_option: str):
    option = [removed_option]
    if removed_option != "--no-prompt-caching":
        option.append("high")
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "grade", "-a", "hw.pdf", "-o", "out", "-S", "subs/",
            *option,
        ])


def test_cli_rejects_out_of_range_values():
    p = build_parser()
    for bad in (["--max-workers", "0"], ["--max-workers", "-2"],
                ["--review-confidence", "5.0"], ["--ocr-threshold", "-0.1"]):
        with pytest.raises(SystemExit):
            p.parse_args(["grade", "-a", "hw.pdf", "-o", "out", "-S", "subs/", *bad])


def test_normalize_rubric_renames_duplicate_criterion_ids():
    """Duplicate criterion ids would collapse in the grader's by-id lookup,
    silently making one criterion ungradable."""
    r = Rubric(problems=[
        RubricProblem(problem_id="1a", points=3.0, criteria=[
            Criterion(id="1a.c1", description="x", points=1.0),
            Criterion(id="1a.c1", description="y", points=2.0)]),
        RubricProblem(problem_id="1b", points=3.0, criteria=[
            Criterion(id="1a.c1", description="z", points=3.0)]),
    ])
    _normalize_rubric(r, {"1a": 3.0, "1b": 3.0}, {"1a", "1b"})
    ids = [c.id for rp in r.problems for c in rp.criteria]
    assert len(ids) == len(set(ids)) == 3
    assert ids[0] == "1a.c1"                       # first keeps its id


def test_cli_max_tokens_applies_to_all_agent_budgets():
    p = build_parser()
    args = p.parse_args(["solve", "-a", "hw.pdf", "-o", "out"])
    cfg = _to_config(args)
    assert cfg.max_tokens == 32768
    assert cfg.big_max_tokens == 32768

    args = p.parse_args([
        "solve", "-a", "hw.pdf", "-o", "out",
        "--max-tokens", "12000",
    ])
    cfg = _to_config(args)
    assert cfg.max_tokens == 32768
    assert cfg.big_max_tokens == 32768

    args = p.parse_args([
        "solve", "-a", "hw.pdf", "-o", "out",
        "--max-tokens", "65536",
    ])
    cfg = _to_config(args)
    assert cfg.max_tokens == 65536
    assert cfg.big_max_tokens == 65536

    with pytest.raises(SystemExit):
        p.parse_args([
            "solve", "-a", "hw.pdf", "-o", "out",
            "--max-tokens", "0",
        ])
