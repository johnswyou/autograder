"""Grading: one fresh grader agent per (student, problem), run in parallel.

The grader receives the rubric criteria, the official solution (ground truth),
the transcript with its OCR confidence, and a crop of the student's work. It
can zoom back into the submission to verify suspicious transcription before
deducting points, and can use ``compute`` to check the student's arithmetic.

``finalize_grade`` is pure post-processing (clamping, totals, forced review
flags) factored out so it can be unit-tested without the API.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from .config import RunConfig
from .ingest import Document
from .llm import UNTRUSTED_CONTENT_NOTE, AgentTask, UsageMeter, run_agent
from .models import (
    ArtifactFailure,
    AssignmentSpec,
    CriterionScore,
    GradeDraft,
    Problem,
    ProblemGrade,
    ProblemLocation,
    ProcessingStatus,
    Rubric,
    RubricProblem,
    Solution,
    SolutionsManual,
    StudentGrade,
    StudentMapping,
    Transcript,
    WorkStatus,
)
from .tools import Block, ToolKit, image_block, text_block

log = logging.getLogger("autograder")

GRADER_SYSTEM = """You are a rigorous, fair grader of physics/math work. You grade ONE problem \
for ONE student, applying a fixed rubric against an official solution.

Ground truth and rubric:
- The OFFICIAL SOLUTION is ground truth for what is correct. The RUBRIC is the law for how \
points are awarded: score each criterion independently, award between 0 and its point value, \
and justify each score with evidence from the student's work.
- Alternative methods: if the student uses a different but VALID method that reaches a correct \
result (or would, but for a slip), award criteria by mapping them onto the equivalent steps of \
that method; explain the mapping in the justification. Never punish a method merely for \
differing from the official one — but verify its logic yourself (use compute) before crediting.
- Follow the rubric's grading_notes for tolerances. Default numeric policy: accept answers \
within reasonable rounding of the official value (and unit-converted equivalents); use compute \
to check equivalence rather than eyeballing.
- Grade what is asked: a correct final answer with required work missing earns only the \
criteria it satisfies; correct work with a transcription artifact is handled below.

Evidence discipline:
- The transcript is your primary evidence but it may contain OCR errors — its confidence score \
is given. Before deducting points for something that looks like a transcription artifact (a \
'1' vs '7', a dropped exponent, an [illegible] span at the crucial step), ZOOM into the \
student's actual work and check. If after zooming the decisive content is still unreadable, do \
NOT guess: set needs_review=true with review_reason and grade what is legible.
- The work crop you are shown comes from the mapper's estimated coordinates and can be \
misaligned — showing a neighbouring problem or empty margin. Judge it against the problem \
statement above: if it is not this problem's work, do not grade from it and do not deduct for \
it. Use view_page on that page to locate this problem yourself, and set needs_review=true \
noting the mislocated region.
- Crossed-out work ([struck: ...]) is not graded unless nothing else exists and the rubric's \
notes say otherwise; note it if relevant.
- Set needs_review=true whenever your decision is materially uncertain (ambiguous work, \
borderline alternative method, suspected mapper error, missing context).

Feedback: 2-5 sentences addressed to the student — what was right, where points were lost and \
why, referencing their actual work. Professional and constructive.""" + UNTRUSTED_CONTENT_NOTE + """

When finished, call submit_result exactly once."""

_AUTO_ZERO = {WorkStatus.blank, WorkStatus.not_found}


def _unavailable_grade(
    problem_id: str,
    status: WorkStatus,
    possible: float,
    stage: str,
    message: str,
    ocr_confidence: float | None,
) -> ProblemGrade:
    return ProblemGrade(
        problem_id=problem_id,
        status=status,
        awarded=None,
        possible=possible,
        criteria=[],
        confidence=0.0,
        needs_review=True,
        review_reason=f"{stage} unavailable: {message}",
        intrinsic_review_reasons=[f"{stage} unavailable: {message}"],
        ocr_confidence=ocr_confidence,
        processing_status=ProcessingStatus.failed,
        failure=ArtifactFailure(stage=stage, message=message, retryable=True),
    )


def finalize_grade(draft: GradeDraft, rp: RubricProblem, leaf_id: str, status: WorkStatus,
                   ocr_confidence: float | None, location_note: str | None,
                   cfg: RunConfig, solution_verified: bool,
                   mapper_flags: int = 0, transcript_flags: int = 0) -> ProblemGrade:
    """Pure post-processing: clamp scores, fill missing criteria, force review flags."""
    by_id = {c.id: c for c in rp.criteria}
    scores: list[CriterionScore] = []
    seen: set[str] = set()
    for cs in draft.criteria:
        crit = by_id.get(cs.criterion_id)
        if crit is None or cs.criterion_id in seen:
            log.warning("grade %s: dropping unknown/duplicate criterion score %r", leaf_id, cs.criterion_id)
            continue
        seen.add(cs.criterion_id)
        awarded = max(0.0, min(float(crit.points), float(cs.awarded)))
        if abs(awarded - cs.awarded) > 1e-9:
            log.warning("grade %s: clamped %s from %g to %g", leaf_id, crit.id, cs.awarded, awarded)
        scores.append(CriterionScore(criterion_id=crit.id, awarded=awarded,
                                     possible=float(crit.points),
                                     justification=cs.justification))
    for cid, crit in by_id.items():
        if cid not in seen:
            log.warning("grade %s: grader omitted criterion %s; scoring 0", leaf_id, cid)
            scores.append(CriterionScore(criterion_id=cid, awarded=0.0, possible=float(crit.points),
                                         justification="Not addressed by grader; defaulted to 0 (review)."))
            draft.needs_review = True
            draft.review_reason = (draft.review_reason or "") + f" grader omitted criterion {cid};"

    grade = ProblemGrade(
        problem_id=leaf_id, status=status,
        criteria=scores, feedback=draft.feedback,
        confidence=draft.confidence, needs_review=draft.needs_review,
        review_reason=draft.review_reason,
        integrity_flags=list(draft.integrity_flags),
        awarded=round(sum(s.awarded for s in scores), 4),
        possible=round(sum(s.possible for s in scores), 4),
        ocr_confidence=ocr_confidence, location_note=location_note,
    )

    # Reasons that describe the work. These are settled here, once, and saved.
    intrinsic: list[str] = []
    if grade.review_reason:
        intrinsic.append(grade.review_reason.strip().strip(";").strip())
    elif grade.needs_review:
        intrinsic.append("grader marked this for review")
    if not solution_verified:
        intrinsic.append("official solution is unverified")
    if grade.integrity_flags or mapper_flags or transcript_flags:
        intrinsic.append("integrity flags present")
    if status == WorkStatus.illegible_candidate:
        intrinsic.append("mapper flagged work as possibly illegible")
    grade.intrinsic_review_reasons = intrinsic

    # The two threshold comparisons are added by apply_review_thresholds, which
    # runs again on every later read of this grade.
    return apply_review_thresholds(grade, cfg)


def apply_review_thresholds(grade: ProblemGrade, cfg: RunConfig) -> ProblemGrade:
    """Derive ``needs_review`` and ``review_reason`` from the current thresholds.

    Exactly two review reasons compare a confidence against a setting rather
    than describing the work: grader confidence against ``review_confidence``,
    and transcript confidence against ``ocr_review_threshold``. Recomputing
    those on every read, instead of trusting what the saved grade recorded, is
    what lets an existing output directory be re-read under a different
    ``--review-confidence`` or ``--ocr-threshold`` without re-grading anything.

    Calling this repeatedly is safe: it always rebuilds both fields from
    ``intrinsic_review_reasons``, never from its own previous output.
    """
    if grade.processing_status is ProcessingStatus.failed:
        # No grader ran, so neither confidence exists to compare. The recorded
        # reason is the failure itself and must survive untouched.
        return grade
    reasons = list(grade.intrinsic_review_reasons)
    if grade.confidence < cfg.review_confidence:
        reasons.append(f"grader confidence {grade.confidence:.2f} < {cfg.review_confidence:.2f}")
    if (grade.ocr_confidence is not None and grade.status not in _AUTO_ZERO
            and grade.ocr_confidence < cfg.ocr_review_threshold):
        reasons.append(f"OCR confidence {grade.ocr_confidence:.2f} < {cfg.ocr_review_threshold:.2f}")
    grade.needs_review = bool(reasons)
    grade.review_reason = "; ".join(reasons) or None
    return grade


def grade_problem(client, cfg: RunConfig, spec: AssignmentSpec, assignment: Document,
                  submission: Document, leaf: Problem, rp: RubricProblem,
                  solution: Solution | None, loc: ProblemLocation, transcript: Transcript,
                  mapper_flags: int = 0, has_unmatched_work: bool = False,
                  meter: UsageMeter | None = None) -> ProblemGrade:
    """Grade one problem for one student."""
    if transcript.processing_status is ProcessingStatus.failed:
        failure = transcript.failure
        assert failure is not None
        return _unavailable_grade(
            leaf.id,
            loc.status,
            rp.points,
            failure.stage,
            failure.message,
            transcript.confidence,
        )

    # a failed-solver placeholder has no content — treat it as no solution at all
    if solution is not None and not (solution.final_answer.strip() or solution.reasoning.strip()):
        solution = None
    sol_verified = bool(solution and solution.verified)

    # blank / not found: deterministic zero, no agent call
    if loc.status in _AUTO_ZERO:
        # A clean blank is a positive observation and stands on its own; the
        # other two shapes of no-work verdict need a human to confirm them.
        doubts: list[str] = []
        if loc.status == WorkStatus.not_found:
            doubts.append(
                "human must confirm the mapper's no-work observation; the submission contains "
                "unattributed work (see mapping.unmatched_work)"
                if has_unmatched_work
                else "human must confirm the mapper's no-work observation"
            )
        if loc.regions:
            # The mapper concluded no work yet still located a region: a scorable
            # zero, but a human confirms that the region really is empty.
            doubts.append("mapper reported no work but located a region; confirm the space is empty")
        draft = GradeDraft(
            criteria=[CriterionScore(criterion_id=c.id, awarded=0.0, possible=float(c.points),
                                     justification=f"No work found (status: {loc.status.value}).")
                      for c in rp.criteria],
            feedback="No response was found for this problem.",
            confidence=1.0, needs_review=bool(doubts),
            review_reason="; ".join(doubts) or None,
        )
        return finalize_grade(draft, rp, leaf.id, loc.status, transcript.confidence,
                              loc.note, cfg, solution_verified=True, mapper_flags=mapper_flags)

    head = [
        f"GRADE problem {leaf.id} ({leaf.label}) — type: {leaf.type.value}, worth {rp.points:g} pt.",
        "\nProblem statement:", spec.stem_text(leaf.id),
    ]
    if leaf.answer_format:
        head.append(f"Expected answer form: {leaf.answer_format}")
    if leaf.choices:
        head.append("Choices: " + "; ".join(f"({chr(65 + i)}) {c}" for i, c in enumerate(leaf.choices)))

    head.append("\nRUBRIC (score every criterion):")
    for c in rp.criteria:
        head.append(f"- {c.id} ({c.points:g} pt): {c.description}")
    if rp.grading_notes:
        head.append(f"Grading notes: {rp.grading_notes}")

    if solution is not None:
        head.append("\nOFFICIAL SOLUTION (ground truth"
                    + ("" if sol_verified else " — UNVERIFIED, treat with care") + "):")
        if solution.reasoning.strip():
            head.append(solution.reasoning.strip())
        head.append(f"Official final answer: {solution.final_answer}")
    else:
        head.append("\n(no official solution available — derive correctness yourself with extra care "
                    "and set needs_review=true unless trivially checkable)")

    head.append(f"\nMAPPER: status={loc.status.value}"
                + (f", note: {loc.note}" if loc.note else "")
                + (f", student's label: {loc.label_seen!r}" if loc.label_seen else ""))
    head.append(f"\nSTUDENT TRANSCRIPT (OCR confidence {transcript.confidence:.2f}"
                + (f", {transcript.illegible_spans} illegible span(s)" if transcript.illegible_spans else "")
                + "):")
    head.append(transcript.text or "(empty)")
    if transcript.quality_notes:
        head.append(f"Transcriber quality notes: {transcript.quality_notes}")
    content: list[Block] = [text_block("\n".join(head))]

    if submission.is_visual and loc.regions:
        r0 = loc.regions[0]
        try:
            jpg = submission.render_region(r0.page, r0.bbox, cfg.zoom_target_edge,
                                           rotate=r0.rotate,
                                           max_upscale=cfg.max_upscale, max_pixels=cfg.max_pixels)
            content.append(text_block(f"[Student's work — submission page {r0.page}; "
                                      f"{len(loc.regions)} region(s) total, zoom doc='submission' for the rest]"))
            content.append(image_block(jpg))
        except Exception as exc:
            log.warning("could not render work crop for %s: %s", leaf.id, exc)
    content.append(text_block("\nGrade now per your instructions."))

    draft = run_agent(client, cfg, AgentTask(
        name="grader", system=GRADER_SYSTEM, user_content=content,
        result_model=GradeDraft,
        toolkit=ToolKit({"submission": submission, "assignment": assignment}, cfg),
        max_tokens=cfg.big_max_tokens, max_turns=cfg.max_agent_turns,
        context=f"{submission.label}/{leaf.id}",
    ), meter)

    return finalize_grade(draft, rp, leaf.id, loc.status, transcript.confidence,
                          loc.note, cfg, solution_verified=sol_verified,
                          mapper_flags=mapper_flags,
                          transcript_flags=len(transcript.integrity_flags))


def aggregate_student_grade(student_id: str, mapping: StudentMapping,
                            transcripts: dict[str, Transcript],
                            grades: dict[str, ProblemGrade]) -> StudentGrade:
    """Deterministic roll-up of per-problem grades into a StudentGrade.

    Factored out of :func:`grade_student` so the orchestrator can re-aggregate
    after retrying individual failed problems on a cached resume."""
    complete = [
        grade
        for grade in grades.values()
        if grade.processing_status is ProcessingStatus.complete
    ]
    score_complete = len(complete) == len(grades)
    processed_awarded = round(sum(grade.awarded or 0.0 for grade in complete), 4)
    processed_possible = round(sum(grade.possible for grade in complete), 4)
    total_possible = round(sum(grade.possible for grade in grades.values()), 4)
    sg = StudentGrade(
        student_id=student_id,
        total_awarded=processed_awarded if score_complete else None,
        total_possible=total_possible,
        processed_awarded=processed_awarded,
        processed_possible=processed_possible,
        score_complete=score_complete,
        problems=grades,
    )
    confs = [t.confidence for pid, t in transcripts.items()
             if t.text and mapping.problems.get(pid, ProblemLocation()).status not in _AUTO_ZERO]
    if confs:
        sg.ocr_confidence_mean = round(sum(confs) / len(confs), 4)
        sg.ocr_confidence_min = round(min(confs), 4)

    if mapping.integrity_flags:
        sg.flags.append("submission integrity flags: " + "; ".join(mapping.integrity_flags))
    for pid, t in transcripts.items():
        for f in t.integrity_flags:
            sg.flags.append(f"{pid} (transcriber): {f}")
    for pid, g in grades.items():
        for f in g.integrity_flags:
            sg.flags.append(f"{pid} (grader): {f}")
    if mapping.extra_pages:
        sg.flags.append(f"extra/inserted pages: {mapping.extra_pages}")
    if mapping.out_of_order:
        sg.flags.append("work appears out of order")
    if mapping.unmatched_work:
        sg.flags.append(f"{len(mapping.unmatched_work)} block(s) of unattributed work — see mapping")
    return sg


def grade_student(client, cfg: RunConfig, spec: AssignmentSpec, assignment: Document,
                  submission: Document, student_id: str, rubric: Rubric,
                  manual: SolutionsManual, mapping: StudentMapping,
                  transcripts: dict[str, Transcript],
                  meter: UsageMeter | None = None,
                  only_ids: set[str] | None = None) -> StudentGrade:
    """Grade every problem (or ``only_ids``) for one student in parallel.

    One problem's agent failure degrades to an unavailable grade forced into
    review (retried on a cached re-run) instead of discarding the sibling
    problems' paid grading work."""
    leaves = {leaf.id: leaf for leaf in spec.leaves()
              if only_ids is None or leaf.id in only_ids}
    mapper_flags = len(mapping.integrity_flags)
    grades: dict[str, ProblemGrade] = {}

    with ThreadPoolExecutor(max_workers=max(1, cfg.max_workers)) as ex:
        futs = {}
        for pid, leaf in leaves.items():
            rp = rubric.for_problem(pid)
            if rp is None:  # complete_rubric guarantees coverage; belt and suspenders
                log.error("no rubric for %s; skipping", pid)
                continue
            loc = mapping.problems.get(pid, ProblemLocation())
            transcript = transcripts.get(pid, Transcript(problem_id=pid))
            futs[pid] = ex.submit(grade_problem, client, cfg, spec, assignment, submission,
                                  leaf, rp, manual.get(pid), loc, transcript,
                                  mapper_flags, bool(mapping.unmatched_work), meter)
        for pid, fut in futs.items():
            try:
                grades[pid] = fut.result()
            except Exception as exc:
                log.error("grading %s/%s FAILED (%s); keeping a flagged unavailable grade "
                          "and continuing — re-run to retry it", student_id, pid, exc)
                rp = rubric.for_problem(pid)
                grades[pid] = _unavailable_grade(
                    pid,
                    mapping.problems.get(pid, ProblemLocation()).status,
                    rp.points if rp else 0.0,
                    "grading",
                    str(exc.__cause__ or exc),
                    transcripts.get(pid, Transcript(problem_id=pid)).confidence,
                )

    return aggregate_student_grade(student_id, mapping, transcripts, grades)
