"""Offline tests for per-problem failure degradation, retry-on-resume, the
generator/evaluator loop, and a full run_grade smoke test — all via the
scripted stub client (no API)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autograder.config import RunConfig
from autograder.grading import grade_problem, grade_student
from autograder.llm import AGENT_FAILURE, SUBMIT_TOOL_NAME
from autograder.models import (
    Criterion,
    ProblemLocation,
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
from autograder.ocr import transcribe_all
from autograder.solutions import generate_manual, solve_problem, validate_and_complete_solutions

from .conftest import make_stub_client, tool_use

DRAFT = {"reasoning": "work it out", "final_answer": "42"}
PASS = {"passed": True, "confidence": 0.9}
FAIL = {"passed": False, "issues": ["sign error in step 2"],
        "fix_suggestions": ["recheck the sign"], "confidence": 0.8}


def _seq_cfg() -> RunConfig:
    # max_workers=1 keeps the futures' execution order deterministic so the
    # scripted responses line up with the agents that consume them
    return RunConfig(api_key="test-key", max_workers=1)


# -- solutions: generator/evaluator loop ---------------------------------------


def test_solve_problem_regenerates_on_rejection(small_spec, tiny_pdf):
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "assignment")
    client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, {"reasoning": "bad", "final_answer": "-42"})],
        [tool_use(SUBMIT_TOOL_NAME, FAIL)],                       # evaluator rejects
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],                      # fresh solver, round 2
        [tool_use(SUBMIT_TOOL_NAME, PASS)],                       # evaluator accepts
    ])
    sol = solve_problem(client, _seq_cfg(), small_spec, doc, small_spec.find("1a"), {}, None)
    doc.close()
    assert sol.verified and sol.rounds == 2 and sol.final_answer == "42"
    # the round-2 solver was told about the rejection
    retry_text = "".join(b["text"] for b in client.calls[2].messages[1]["content"]
                         if isinstance(b, dict) and b.get("type") == "text")
    assert "REJECTED" in retry_text and "sign error in step 2" in retry_text


def test_solution_max_rounds_counts_regenerations_after_initial_attempt(
    small_spec,
    tiny_pdf,
):
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "assignment")
    client = make_stub_client(
        [
            [tool_use(SUBMIT_TOOL_NAME, DRAFT)],
            [tool_use(SUBMIT_TOOL_NAME, FAIL)],
            [tool_use(SUBMIT_TOOL_NAME, DRAFT)],
            [tool_use(SUBMIT_TOOL_NAME, FAIL)],
            [tool_use(SUBMIT_TOOL_NAME, DRAFT)],
            [tool_use(SUBMIT_TOOL_NAME, FAIL)],
        ]
    )
    cfg = RunConfig(
        api_key="test-key",
        max_workers=1,
        solution_max_rounds=2,
    )

    sol = solve_problem(
        client,
        cfg,
        small_spec,
        doc,
        small_spec.find("1a"),
        {},
        None,
    )
    doc.close()

    assert not sol.verified
    assert sol.rounds == cfg.solution_max_rounds + 1
    assert len(client.calls) == 2 * (cfg.solution_max_rounds + 1)
    # the last draft is kept, flagged with the reviewer's reason for rejecting it
    assert sol.final_answer == "42"
    assert sol.verifier_notes is not None
    assert "UNRESOLVED after max rounds" in sol.verifier_notes
    assert "sign error in step 2" in sol.verifier_notes


def test_solve_problem_with_zero_rounds_makes_exactly_one_attempt(small_spec, tiny_pdf):
    """``solution_max_rounds=0`` is the documented minimum: solve once, verify
    once, no regeneration. It is also the boundary of the round loop, so a
    rejected single attempt must still come back as a flagged solution rather
    than falling off the end of the loop with no draft to return."""
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "assignment")
    client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],
        [tool_use(SUBMIT_TOOL_NAME, FAIL)],
    ])
    cfg = RunConfig(api_key="test-key", max_workers=1, solution_max_rounds=0)

    sol = solve_problem(client, cfg, small_spec, doc, small_spec.find("1a"), {}, None)
    doc.close()

    assert len(client.calls) == 2, "one solver call and one evaluator call, no regeneration"
    assert sol.rounds == 1
    assert not sol.verified
    assert sol.final_answer == "42"
    assert sol.verifier_notes is not None and "UNRESOLVED after max rounds" in sol.verifier_notes


def test_solve_problem_keeps_draft_when_evaluator_dies(small_spec, tiny_pdf):
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "assignment")
    client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],
        RuntimeError("api down"),                                 # evaluator dies
    ])
    sol = solve_problem(client, _seq_cfg(), small_spec, doc, small_spec.find("1a"), {}, None)
    doc.close()
    assert not sol.verified
    assert sol.final_answer == "42"                               # draft preserved
    assert sol.verifier_notes.startswith(AGENT_FAILURE)


def test_generate_manual_taints_dependent_of_unverified_prerequisite(small_spec, tiny_pdf):
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "assignment")
    client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],                      # solver for 1a
        RuntimeError("evaluator unavailable"),                   # evaluator for 1a
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],                      # solver for 1b
        [tool_use(SUBMIT_TOOL_NAME, PASS)],                       # evaluator for 1b
    ])

    manual = generate_manual(
        client,
        _seq_cfg(),
        small_spec,
        doc,
        only_ids={"1a", "1b"},
        meter=None,
    )
    doc.close()

    assert not manual.solutions["1a"].verified
    assert not manual.solutions["1b"].verified
    assert manual.solutions["1b"].unverified_dependencies == ["1a"]

    dependent_solver_text = "".join(
        block["text"]
        for block in client.calls[2].messages[1]["content"]
        if isinstance(block, dict) and block.get("type") == "text"
    )
    assert "UNVERIFIED PREREQUISITE" in dependent_solver_text
    assert "OFFICIAL RESULTS OF PREREQUISITE PARTS" not in dependent_solver_text


def test_generate_manual_taints_dependent_of_failed_solver(small_spec, tiny_pdf):
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "assignment")
    client = make_stub_client([
        RuntimeError("solver unavailable"),                      # solver for 1a
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],                      # solver for 1b
        [tool_use(SUBMIT_TOOL_NAME, PASS)],                       # evaluator for 1b
    ])

    manual = generate_manual(
        client,
        _seq_cfg(),
        small_spec,
        doc,
        only_ids={"1a", "1b"},
        meter=None,
    )
    doc.close()

    assert not manual.solutions["1a"].verified
    assert not manual.solutions["1b"].verified
    assert manual.solutions["1b"].unverified_dependencies == ["1a"]


def test_generate_manual_solver_failure_preserves_dependency_blockers(small_spec, tiny_pdf):
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "assignment")
    prerequisite = Solution(
        problem_id="1a",
        final_answer="draft-a",
        verified=False,
        unverified_dependencies=["0"],
        verifier_notes=f"{AGENT_FAILURE} evaluator agent failed: unavailable",
    )
    client = make_stub_client([RuntimeError("solver unavailable")])

    manual = generate_manual(
        client,
        _seq_cfg(),
        small_spec,
        doc,
        only_ids={"1b"},
        known={"1a": prerequisite},
        meter=None,
    )
    doc.close()

    failed = manual.solutions["1b"]
    assert not failed.verified
    assert failed.unverified_dependencies == ["0", "1a"]
    assert "UNVERIFIED PREREQUISITE DEPENDENCIES: 0, 1a" in failed.verifier_notes


def test_provided_solution_keeps_matching_status_when_requested_check_dies(
    small_spec,
    tiny_pdf,
):
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "assignment")
    provided = {
        problem_id: Solution(
            problem_id=problem_id,
            final_answer="42",
            verified=True,
            provenance="provided",
        )
        for problem_id in small_spec.leaf_ids()
    }
    client = make_stub_client(
        [RuntimeError("evaluator unavailable") for _ in provided]
    )
    cfg = RunConfig(
        api_key="test-key",
        max_workers=1,
        verify_provided_solutions=True,
    )

    manual, issues = validate_and_complete_solutions(
        client,
        cfg,
        small_spec,
        doc,
        provided,
        None,
    )
    doc.close()

    assert all(solution.verified for solution in manual.solutions.values())
    assert len(issues) == len(provided)
    assert all(
        issue.message.startswith("could not verify provided solution")
        for issue in issues
    )


def test_provided_verification_taints_generated_dependent(small_spec, tiny_pdf):
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "assignment")
    provided = {
        "1a": Solution(
            problem_id="1a",
            final_answer="7.7 s",
            verified=True,
            provenance="provided",
        ),
        "2": Solution(
            problem_id="2",
            final_answer="B",
            verified=True,
            provenance="provided",
        ),
    }
    client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],                      # generated 1b solver
        [tool_use(SUBMIT_TOOL_NAME, PASS)],                       # generated 1b evaluator
        [tool_use(SUBMIT_TOOL_NAME, FAIL)],                       # provided 1a evaluator
        [tool_use(SUBMIT_TOOL_NAME, PASS)],                       # provided 2 evaluator
    ])
    cfg = RunConfig(
        api_key="test-key",
        max_workers=1,
        verify_provided_solutions=True,
    )

    manual, _ = validate_and_complete_solutions(
        client,
        cfg,
        small_spec,
        doc,
        provided,
        None,
    )
    doc.close()

    assert not manual.solutions["1a"].verified
    assert not manual.solutions["1b"].verified
    assert manual.solutions["1b"].unverified_dependencies == ["1a"]


def test_generate_manual_degrades_per_problem(small_spec, tiny_pdf):
    """One problem's failure must not abort the stage or starve dependents."""
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "assignment")
    client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],                      # 1a solver
        [tool_use(SUBMIT_TOOL_NAME, PASS)],                       # 1a evaluator
        RuntimeError("boom"),                                     # 2 solver dies
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],                      # 1b solver (level 2)
        [tool_use(SUBMIT_TOOL_NAME, PASS)],                       # 1b evaluator
    ])
    manual = generate_manual(client, _seq_cfg(), small_spec, doc, meter=None)
    doc.close()
    assert manual.solutions["1a"].verified
    assert manual.solutions["1b"].verified                        # dependent still solved
    failed = manual.solutions["2"]
    assert not failed.verified and failed.verifier_notes.startswith(AGENT_FAILURE)
    # 1b's solver saw its dependency's official result
    dep_text = "".join(b["text"] for b in client.calls[3].messages[1]["content"]
                       if isinstance(b, dict) and b.get("type") == "text")
    assert "OFFICIAL RESULTS OF VERIFIED PREREQUISITE PARTS" in dep_text and "42" in dep_text


def test_gap_generation_receives_provided_dependencies(small_spec, tiny_pdf):
    """Generating a missing part must receive prerequisite results that came
    from the teacher's key, not only from freshly generated ones."""
    from autograder.ingest import Document

    doc = Document.from_path(tiny_pdf, "assignment")
    provided = {
        "1a": Solution(problem_id="1a", final_answer="7.7 s", verified=True, provenance="provided"),
        "2": Solution(problem_id="2", final_answer="B", verified=True, provenance="provided"),
    }
    client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],                      # 1b solver (the gap)
        [tool_use(SUBMIT_TOOL_NAME, PASS)],                       # 1b evaluator
    ])
    manual, issues = validate_and_complete_solutions(
        client, _seq_cfg(), small_spec, doc, provided, None)
    doc.close()
    assert manual.solutions["1b"].verified
    assert any("INCOMPLETE" in i.message for i in issues)
    solver_text = "".join(b["text"] for b in client.calls[0].messages[1]["content"]
                          if isinstance(b, dict) and b.get("type") == "text")
    assert "PREREQUISITE" in solver_text and "7.7 s" in solver_text


# -- transcripts / grades degrade per problem ----------------------------------


def _mapping_two_answered() -> StudentMapping:
    return StudentMapping(page_count=2, problems={
        "1a": ProblemLocation(status=WorkStatus.answered,
                              regions=[Region(page=1, bbox=[0, 0, 90, 40])]),
        "1b": ProblemLocation(status=WorkStatus.answered,
                              regions=[Region(page=1, bbox=[0, 40, 90, 80])]),
        "2": ProblemLocation(status=WorkStatus.blank),
    })


def test_transcribe_all_degrades_per_problem(small_spec, tiny_pdf):
    from autograder.ingest import Document
    from autograder.models import ArtifactFailure, ProcessingStatus

    doc = Document.from_path(tiny_pdf, "s1")
    client = make_stub_client([
        RuntimeError("api down"),                                 # 1a transcriber dies
        [tool_use(SUBMIT_TOOL_NAME, {"text": "v = 9.8t", "confidence": 0.9})],  # 1b ok
    ])                                                            # 2 is blank: no call
    out = transcribe_all(client, _seq_cfg(), small_spec, doc, _mapping_two_answered(), None)
    doc.close()
    assert out["1b"].text == "v = 9.8t"
    failed = out["1a"]
    assert failed.processing_status is ProcessingStatus.failed
    assert failed.failure == ArtifactFailure(
        stage="transcription",
        message="api down",
        retryable=True,
    )
    assert failed.text == ""
    assert failed.confidence == 0.0
    assert out["2"].quality_notes and "no work to transcribe" in out["2"].quality_notes


def test_grade_student_degrades_per_problem(small_spec, tiny_pdf, cfg):
    from autograder.ingest import Document
    from autograder.models import ArtifactFailure, ProcessingStatus

    doc = Document.from_path(tiny_pdf, "s1")
    rubric = Rubric(problems=[
        RubricProblem(problem_id="1a", points=3.0, criteria=[
            Criterion(id="1a.c1", description="all", points=3.0)]),
        RubricProblem(problem_id="1b", points=3.0, criteria=[
            Criterion(id="1b.c1", description="all", points=3.0)]),
        RubricProblem(problem_id="2", points=4.0, criteria=[
            Criterion(id="2.c1", description="choice", points=4.0)]),
    ])
    transcripts = {
        "1a": Transcript(problem_id="1a", text="t = 3.03", confidence=0.9),
        "1b": Transcript(problem_id="1b", text="v = 29.7", confidence=0.9),
        "2": Transcript(problem_id="2"),
    }
    manual = SolutionsManual(solutions={
        pid: Solution(problem_id=pid, final_answer="42", verified=True) for pid in ("1a", "1b", "2")})
    client = make_stub_client([
        RuntimeError("boom"),                                     # 1a grader dies
        [tool_use(SUBMIT_TOOL_NAME, {"criteria": [
            {"criterion_id": "1b.c1", "awarded": 3.0, "possible": 3.0, "justification": "ok"}],
            "feedback": "good", "confidence": 0.9})],             # 1b ok
    ])                                                            # 2 is blank: auto-zero
    cfg1 = _seq_cfg()
    sg = grade_student(client, cfg1, small_spec, doc, doc, "alice", rubric, manual,
                       _mapping_two_answered(), transcripts, None)
    doc.close()
    assert sg.problems["1b"].awarded == 3.0
    assert sg.problems["2"].awarded == 0.0                        # deterministic zero
    failed = sg.problems["1a"]
    assert failed.processing_status is ProcessingStatus.failed
    assert failed.failure == ArtifactFailure(
        stage="grading",
        message="boom",
        retryable=True,
    )
    assert failed.awarded is None
    assert failed.criteria == []
    assert failed.possible == 3.0
    assert failed.needs_review
    assert sg.total_awarded is None
    assert sg.processed_awarded == 3.0
    assert sg.processed_possible == 7.0
    assert not sg.score_complete
    assert sg.total_possible == 10.0                              # siblings kept


def test_failed_transcript_skips_grader_agent(small_spec, tiny_pdf):
    from autograder.ingest import Document
    from autograder.models import ArtifactFailure, ProcessingStatus

    doc = Document.from_path(tiny_pdf, "s1")
    client = make_stub_client([])
    failed_transcript = Transcript(
        problem_id="1a",
        text="",
        confidence=0.0,
        processing_status=ProcessingStatus.failed,
        failure=ArtifactFailure(stage="transcription", message="api down", retryable=True),
    )
    rubric_problem = RubricProblem(
        problem_id="1a",
        points=3.0,
        criteria=[Criterion(id="1a.c1", description="all", points=3.0)],
    )

    grade = grade_problem(
        client, _seq_cfg(), small_spec, doc, doc, small_spec.find("1a"), rubric_problem,
        Solution(problem_id="1a", final_answer="42", verified=True),
        _mapping_two_answered().problems["1a"], failed_transcript,
    )
    doc.close()

    assert client.calls == []
    assert grade.processing_status is ProcessingStatus.failed
    assert grade.failure is not None
    assert grade.failure.stage == "transcription"


def test_stage_student_retries_structured_transcript_and_grade_failures(
    tmp_path: Path, small_spec, tiny_pdf,
):
    from autograder.models import ArtifactFailure, ProcessingStatus
    from autograder.orchestrator import Pipeline, _Transcripts
    from autograder.report import save_json

    out = tmp_path / "run"
    pipe = Pipeline(_seq_cfg(), tiny_pdf, out)
    student_dir = out / "students" / "alice"
    mapping = _mapping_two_answered()
    failed_transcript = Transcript(
        problem_id="1a",
        text="",
        confidence=0.0,
        processing_status=ProcessingStatus.failed,
        failure=ArtifactFailure(stage="transcription", message="api down", retryable=True),
    )
    transcripts = {
        "1a": failed_transcript,
        "1b": Transcript(problem_id="1b", text="v = 29.7", confidence=0.9),
        "2": Transcript(problem_id="2"),
    }
    failed_grade = {
        "1a": {
            "problem_id": "1a",
            "status": WorkStatus.answered,
            "awarded": None,
            "possible": 3.0,
            "criteria": [],
            "needs_review": True,
            "processing_status": ProcessingStatus.failed,
            "failure": ArtifactFailure(stage="grading", message="boom"),
        },
        "1b": {
            "problem_id": "1b",
            "status": WorkStatus.answered,
            "awarded": 3.0,
            "possible": 3.0,
        },
        "2": {
            "problem_id": "2",
            "status": WorkStatus.blank,
            "awarded": 0.0,
            "possible": 4.0,
        },
    }
    cached_grade = StudentGrade(
        student_id="alice",
        total_awarded=None,
        total_possible=10.0,
        processed_awarded=3.0,
        processed_possible=7.0,
        score_complete=False,
        problems=failed_grade,
    )
    rubric = Rubric(problems=[
        RubricProblem(problem_id="1a", points=3.0, criteria=[
            Criterion(id="1a.c1", description="all", points=3.0)]),
        RubricProblem(problem_id="1b", points=3.0, criteria=[
            Criterion(id="1b.c1", description="all", points=3.0)]),
        RubricProblem(problem_id="2", points=4.0, criteria=[
            Criterion(id="2.c1", description="choice", points=4.0)]),
    ])
    manual = SolutionsManual(solutions={
        pid: Solution(problem_id=pid, final_answer="42", verified=True)
        for pid in ("1a", "1b", "2")
    })
    save_json(student_dir / "mapping.json", mapping)
    save_json(student_dir / "transcripts.json", _Transcripts(transcripts=transcripts))
    save_json(student_dir / "grades.json", cached_grade)

    pipe._client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, {"text": "t = 3.03", "confidence": 0.9})],
        [tool_use(SUBMIT_TOOL_NAME, {"criteria": [
            {"criterion_id": "1a.c1", "awarded": 3.0, "possible": 3.0,
             "justification": "ok"}],
            "feedback": "good", "confidence": 0.9})],
    ])
    grade = pipe.stage_student(small_spec, rubric, manual, "alice", [tiny_pdf])
    pipe.assignment.close()

    assert grade.problems["1a"].processing_status is ProcessingStatus.complete
    assert grade.problems["1a"].failure is None
    assert grade.score_complete
    assert grade.total_awarded == 6.0


# -- orchestrator: retry-on-resume ----------------------------------------------


def test_stage_solutions_retries_failed_solution_and_transitive_dependents(
    tmp_path: Path, small_spec, tiny_pdf,
):
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    pipe = Pipeline(RunConfig(api_key="test-key", max_workers=1), tiny_pdf, out)
    cached = SolutionsManual(assignment_title="Quiz", solutions={
        "1a": Solution(
            problem_id="1a",
            final_answer="stale-a",
            verified=False,
            verifier_notes=f"{AGENT_FAILURE} solver agent failed: boom",
        ),
        "1b": Solution(problem_id="1b", final_answer="stale-b", verified=True),
        "2": Solution(problem_id="2", final_answer="unrelated", verified=True),
    })
    save_json(out / "solutions_manual.json", cached)

    fresh_a = {"reasoning": "fresh work for a", "final_answer": "fresh-a"}
    fresh_b = {"reasoning": "fresh work for b", "final_answer": "fresh-b"}
    pipe._client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, fresh_a)],                    # regenerated 1a
        [tool_use(SUBMIT_TOOL_NAME, PASS)],
        [tool_use(SUBMIT_TOOL_NAME, fresh_b)],                    # regenerated 1b
        [tool_use(SUBMIT_TOOL_NAME, PASS)],
    ])

    manual = pipe.stage_solutions(small_spec, None)
    pipe.assignment.close()

    assert manual.solutions["1a"].final_answer == "fresh-a"
    assert manual.solutions["1b"].final_answer == "fresh-b"
    assert manual.solutions["2"].final_answer == "unrelated"
    on_disk = SolutionsManual.model_validate_json((out / "solutions_manual.json").read_text())
    assert on_disk == manual


def test_solution_repair_invalidates_derived_artifacts_before_publish(
    tmp_path: Path, small_spec, tiny_pdf, monkeypatch,
):
    import autograder.orchestrator as orchestrator
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    pipe = Pipeline(RunConfig(api_key="test-key", max_workers=1), tiny_pdf, out)
    cached = SolutionsManual(assignment_title="Quiz", solutions={
        "1a": Solution(
            problem_id="1a",
            final_answer="stale-a",
            verified=False,
            verifier_notes=f"{AGENT_FAILURE} solver agent failed: boom",
        ),
    })
    save_json(out / "solutions_manual.json", cached)

    derived = [
        out / "rubric.json",
        out / "rubric.md",
        out / "students" / "alice" / "grades.json",
        out / "students" / "alice" / "report.md",
        out / "summary.csv",
        out / "review_queue.md",
        out / "run_manifest.json",
    ]
    retained = [
        out / "students" / "alice" / "mapping.json",
        out / "students" / "alice" / "transcripts.json",
    ]
    for artifact in derived + retained:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("cached", encoding="utf-8")

    replacement = SolutionsManual(assignment_title="Quiz", solutions={
        "1a": Solution(problem_id="1a", final_answer="fresh-a", verified=True),
    })
    monkeypatch.setattr(orchestrator, "generate_manual", lambda *args, **kwargs: replacement)

    def fail_json_publish(path, model):
        if path.name == "solutions_manual.json":
            raise RuntimeError("simulated publication failure")

    monkeypatch.setattr(orchestrator, "save_json", fail_json_publish)
    try:
        with pytest.raises(RuntimeError, match="simulated publication failure"):
            pipe.stage_solutions(small_spec, None)
    finally:
        pipe.assignment.close()

    assert all(not artifact.exists() for artifact in derived)
    assert all(artifact.exists() for artifact in retained)


def test_stage_solutions_retries_marked_failures(tmp_path: Path, small_spec, tiny_pdf):
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    pipe = Pipeline(RunConfig(api_key="test-key", max_workers=1), tiny_pdf, out)
    cached = SolutionsManual(assignment_title="Quiz", solutions={
        "1a": Solution(problem_id="1a", final_answer="ok", verified=True),
        "1b": Solution(problem_id="1b", final_answer="ok", verified=True),
        "2": Solution(problem_id="2", verified=False,
                      verifier_notes=f"{AGENT_FAILURE} solver agent failed: boom"),
    })
    save_json(out / "solutions_manual.json", cached)

    pipe._client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],                      # retried solver for 2
        [tool_use(SUBMIT_TOOL_NAME, PASS)],                       # its evaluator
    ])
    manual = pipe.stage_solutions(small_spec, None)
    pipe.assignment.close()
    assert manual.solutions["2"].verified
    assert not any("failed in a previous run" in i.message for i in pipe.issues)
    # the retried result was persisted
    on_disk = SolutionsManual.model_validate_json((out / "solutions_manual.json").read_text())
    assert on_disk.solutions["2"].verified


def test_stage_solutions_keeps_placeholders_without_key(tmp_path: Path, small_spec, tiny_pdf):
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    cached = SolutionsManual(solutions={
        "1a": Solution(problem_id="1a", final_answer="ok", verified=True),
        "1b": Solution(problem_id="1b", final_answer="ok", verified=True),
        "2": Solution(problem_id="2", verified=False,
                      verifier_notes=f"{AGENT_FAILURE} solver agent failed: boom"),
    })
    save_json(out / "solutions_manual.json", cached)

    manual = pipe.stage_solutions(small_spec, None)
    pipe.assignment.close()
    assert not manual.solutions["2"].verified                     # placeholder kept
    assert pipe._client is None                                   # no client constructed
    assert any("failed in a previous run" in i.message for i in pipe.issues)


def test_pipeline_close_closes_created_client_once(tmp_path: Path, tiny_pdf):
    from autograder.orchestrator import Pipeline

    pipe = Pipeline(RunConfig(api_key="test-key"), tiny_pdf, tmp_path / "run")
    client = make_stub_client([])
    pipe._client = client
    pipe.close()
    pipe.close()
    assert client.close_calls == 1


@pytest.mark.parametrize("fails", [False, True])
def test_pipeline_entry_point_closes_created_client_on_every_exit(
    tmp_path: Path, tiny_pdf, small_spec, monkeypatch, fails: bool,
):
    from autograder.orchestrator import Pipeline

    pipe = Pipeline(RunConfig(api_key="test-key"), tiny_pdf, tmp_path / "run")
    client = make_stub_client([])
    pipe._client = client
    if fails:
        monkeypatch.setattr(
            pipe,
            "stage_spec",
            lambda: (_ for _ in ()).throw(RuntimeError("stage failed")),
        )
        with pytest.raises(RuntimeError, match="stage failed"):
            pipe.run_inspect()
    else:
        monkeypatch.setattr(pipe, "stage_spec", lambda: small_spec)
        assert pipe.run_inspect() == small_spec
    assert client.close_calls == 1


# -- full pipeline smoke ---------------------------------------------------------


def test_run_grade_raises_partial_after_writing_outputs(
    tmp_path: Path, small_spec, tiny_pdf, monkeypatch,
):
    import autograder.orchestrator as orchestrator
    from autograder.models import ArtifactFailure, ProblemGrade, ProcessingStatus, StudentFailure
    from autograder.orchestrator import PartialGradeFailure

    out = tmp_path / "run"
    pipe = orchestrator.Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    incomplete = StudentGrade(
        student_id="alice",
        total_awarded=None,
        total_possible=10.0,
        processed_awarded=3.0,
        processed_possible=3.0,
        score_complete=False,
        problems={
            "1a": ProblemGrade(
                problem_id="1a",
                awarded=None,
                possible=3.0,
                processing_status=ProcessingStatus.failed,
                failure=ArtifactFailure(stage="grading", message="grader unavailable"),
            ),
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "discover_submissions",
        lambda paths: [("alice", [tiny_pdf]), ("bob", [tiny_pdf])],
    )
    monkeypatch.setattr(pipe, "stage_spec", lambda: small_spec)
    monkeypatch.setattr(pipe, "stage_solutions", lambda spec, path: SolutionsManual())
    monkeypatch.setattr(pipe, "stage_rubric", lambda spec, manual, path, steer: Rubric())

    def stage_student(spec, rubric, manual, student_id, files):
        if student_id == "alice":
            return incomplete
        raise RuntimeError("unreadable submission")

    monkeypatch.setattr(pipe, "stage_student", stage_student)

    with pytest.raises(PartialGradeFailure) as caught:
        pipe.run_grade([tiny_pdf], None, None, None)

    assert caught.value.grades == [incomplete]
    assert caught.value.failures == [
        StudentFailure(
            student_id="bob",
            stage="student",
            message="unreadable submission",
        ),
    ]
    for artifact in ("summary.csv", "review_queue.md", "run_manifest.json"):
        assert (out / artifact).exists()
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_status"] == "partial_failure"
    summary = (out / "summary.csv").read_text(encoding="utf-8")
    review_queue = (out / "review_queue.md").read_text(encoding="utf-8")
    assert all(student_id in summary for student_id in ("alice", "bob"))
    assert all(student_id in review_queue for student_id in ("alice", "bob"))


def test_run_grade_smoke(tmp_path: Path, tiny_pdf):
    """End-to-end run_grade over the stub client: every stage, every artifact."""
    from autograder.orchestrator import Pipeline

    out = tmp_path / "run"
    client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, {                             # 1. spec
            "title": "HW", "total_points": 2.0,
            "problems": [{"id": "1", "label": "Problem 1", "prompt": "compute 2+2",
                          "type": "numeric", "points": 2.0, "pages": [1]}]})],
        [tool_use(SUBMIT_TOOL_NAME, DRAFT)],                      # 2. solver
        [tool_use(SUBMIT_TOOL_NAME, PASS)],                       # 3. evaluator
        [tool_use(SUBMIT_TOOL_NAME, {                             # 4. rubric
            "title": "R", "total_points": 2.0,
            "problems": [{"problem_id": "1", "points": 2.0, "criteria": [
                {"id": "1.c1", "description": "correct value", "points": 2.0}]}]})],
        [tool_use(SUBMIT_TOOL_NAME, {                             # 5. mapper
            "page_count": 2,
            "problems": {"1": {"status": "answered",
                               "regions": [{"page": 1, "bbox": [0, 0, 100, 60]}]}}})],
        [tool_use(SUBMIT_TOOL_NAME, {"text": "2+2=4", "confidence": 0.95})],  # 6. transcriber
        [tool_use(SUBMIT_TOOL_NAME, {"criteria": [                # 7. grader
            {"criterion_id": "1.c1", "awarded": 2.0, "possible": 2.0, "justification": "correct"}],
            "feedback": "well done", "confidence": 0.95})],
    ])
    pipe = Pipeline(RunConfig(api_key="test-key", max_workers=1), tiny_pdf, out)
    pipe._client = client
    grades = pipe.run_grade([tiny_pdf], None, None, None)

    assert len(grades) == 1
    g = grades[0]
    assert g.student_id == "tiny"
    assert g.total_awarded == 2.0 and g.total_possible == 2.0
    assert not any(p.needs_review for p in g.problems.values())
    assert pipe.meter.snapshot()["api_calls"] == 7

    for rel in ("assignment_spec.json", "solutions_manual.json", "solutions_manual.md",
                "rubric.json", "rubric.md", "summary.csv", "review_queue.md",
                "run_manifest.json", "students/tiny/mapping.json",
                "students/tiny/transcripts.json", "students/tiny/grades.json",
                "students/tiny/report.md"):
        assert (out / rel).exists(), f"missing artifact: {rel}"
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_status"] == "complete"
    assert r"2\+2\=4" in (out / "students/tiny/report.md").read_text()
    assert "Nothing was flagged" in (out / "review_queue.md").read_text()

    # --- re-read the finished run under a stricter review threshold ----------
    # The thresholds are not part of the run binding, so this must be accepted
    # on the same directory, cost nothing, and re-flag from the saved grades.
    strict = Pipeline(
        RunConfig(api_key=None, max_workers=1, review_confidence=0.99), tiny_pdf, out,
    )
    regraded = strict.run_grade([tiny_pdf], None, None, None)

    assert strict.meter.snapshot()["api_calls"] == 0, "re-flagging must not call the API"
    flagged = regraded[0].problems["1"]
    assert flagged.needs_review
    assert "grader confidence 0.95 < 0.99" in flagged.review_reason
    assert flagged.awarded == 2.0, "the score itself must be untouched"
    assert regraded[0].total_awarded == 2.0
    assert "Nothing was flagged" not in (out / "review_queue.md").read_text()

    # and back: relaxing it again clears the mark, still without an API call
    relaxed = Pipeline(
        RunConfig(api_key=None, max_workers=1, review_confidence=0.60), tiny_pdf, out,
    )
    restored = relaxed.run_grade([tiny_pdf], None, None, None)
    assert relaxed.meter.snapshot()["api_calls"] == 0
    assert not restored[0].problems["1"].needs_review
    assert "Nothing was flagged" in (out / "review_queue.md").read_text()
