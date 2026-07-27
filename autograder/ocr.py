"""Student pass, stage 2: transcribe each problem's work with OCR confidence.

One fresh transcription agent per problem (run in parallel). The agent sees
the cropped region(s) the mapper located, can zoom further on its own, and
must transcribe VERBATIM — including the student's mistakes — with explicit
``[illegible]`` markers and a calibrated confidence score. That confidence
flows into grading: low-confidence transcripts force human review rather than
silently grading garbled text.
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
    Problem,
    ProblemLocation,
    ProcessingStatus,
    StudentMapping,
    Transcript,
    TranscriptDraft,
    WorkStatus,
)
from .tools import Block, ToolKit, image_block, text_block

log = logging.getLogger("autograder")

TRANSCRIBER_SYSTEM = """You are a meticulous transcriber of handwritten and typed student work \
in math and physics, producing the exact text a grader will judge.

Rules:
- Transcribe VERBATIM. Reproduce the student's work exactly as written — including arithmetic \
mistakes, wrong formulas, misspellings, and abandoned attempts. NEVER correct, complete, or \
'clean up' the work: the grader must see what the student actually wrote.
- Write math in LaTeX (inline $...$). Preserve layout meaning: keep equation sequences in order, \
note column/table structure, and describe non-text content in brackets, e.g. [diagram: block on \
incline, arrows labeled N up-left, mg down], [graph: concave-up curve through origin].
- Crossed-out work: transcribe as [struck: ...] — it is usually not graded but the grader \
decides.
- CHECK THE CROP FIRST. The work regions you are shown come from the mapper's estimated \
coordinates and can be misaligned — landing on a neighbouring problem, or on empty margin. \
Before transcribing, confirm the region really holds the problem named above; if it does not, \
use view_page on that page, find this problem yourself, and transcribe from there. Say so in \
quality_notes when you had to relocate.
- Illegibility: zoom FIRST (multiple regions, high magnification, try rotate for sideways \
margins). If still unreadable, write [illegible] or [illegible: best-guess?] with a '?' — never \
silently guess. Count such spans in illegible_spans.
- confidence is your calibrated estimate that the transcription is essentially error-free:
  ~0.95+: clean print/typeset or very neat ink, fully read.
  ~0.85: neat handwriting, a couple of uncertain characters.
  ~0.7: messy but mostly readable; several guesses.
  ~0.5: substantial uncertain portions; grading may be unreliable.
  <0.3: largely unreadable (faint pencil, blur, low resolution).
- quality_notes: state WHY confidence is reduced (pencil too faint, photo blur, page cut off, \
ink bleed), so a human knows what to re-scan.""" + UNTRUSTED_CONTENT_NOTE + """

When finished, call submit_result exactly once."""

_SKIP_STATUSES = {WorkStatus.blank, WorkStatus.not_found}


def transcribe_problem(client, cfg: RunConfig, spec: AssignmentSpec, submission: Document,
                       leaf: Problem, loc: ProblemLocation,
                       meter: UsageMeter | None = None) -> Transcript:
    """Transcribe one problem's work. Blank/not-found problems short-circuit."""
    if loc.status is WorkStatus.mapping_error:
        return Transcript(
            problem_id=leaf.id,
            processing_status=ProcessingStatus.failed,
            failure=ArtifactFailure(
                stage="mapping",
                message=loc.note or "mapper could not reliably locate this problem's work",
                retryable=True,
            ),
        )
    if loc.status in _SKIP_STATUSES or not loc.regions:
        return Transcript(problem_id=leaf.id, text="", confidence=1.0 if loc.status in _SKIP_STATUSES else 0.0,
                          quality_notes=f"no work to transcribe (status: {loc.status.value})")

    intro = [
        f"Transcribe the student's work for problem {leaf.id} ({leaf.label}).",
        "Problem statement (for context only — do NOT copy it into the transcription):",
        spec.stem_text(leaf.id),
    ]
    if leaf.answer_format:
        intro.append(f"Expected answer form: {leaf.answer_format}")
    if loc.note:
        intro.append(f"Mapper note: {loc.note}")
    if loc.label_seen:
        intro.append(f"Student's own label on this work: {loc.label_seen!r}")
    content: list[Block] = [text_block("\n".join(intro))]

    if submission.is_visual:
        for k, region in enumerate(loc.regions[:8], 1):
            try:
                jpg = submission.render_region(region.page, region.bbox, cfg.zoom_target_edge,
                                               rotate=region.rotate,
                                               max_upscale=cfg.max_upscale, max_pixels=cfg.max_pixels)
                content.append(text_block(
                    f"[Work region {k}/{min(len(loc.regions), 8)} — submission page {region.page}]"))
                content.append(image_block(jpg))
            except Exception as exc:
                log.warning("could not render region %d for %s: %s", k, leaf.id, exc)
        content.append(text_block(
            "Use zoom on doc='submission' for anything unclear (you may zoom outside the shown "
            "regions if work continues past their edges). Then transcribe per your instructions."
        ))
    else:
        pages = sorted({r.page for r in loc.regions}) or list(range(1, submission.n_pages + 1))
        for page in pages[:6]:
            txt = submission.page_text(page)
            if txt:
                content.append(text_block(f"[Submission chunk {page} source]\n{txt}"))
        content.append(text_block(
            "The submission is a text document; extract this problem's portion verbatim "
            "(confidence is normally ~1.0 unless the source itself is ambiguous)."
        ))

    draft: TranscriptDraft = run_agent(client, cfg, AgentTask(
        name="transcriber", system=TRANSCRIBER_SYSTEM, user_content=content,
        result_model=TranscriptDraft, toolkit=ToolKit({"submission": submission}, cfg),
        tool_names=("view_page", "zoom", "read_text"),
        max_tokens=cfg.big_max_tokens, max_turns=cfg.max_agent_turns,
        context=f"{submission.label}/{leaf.id}",
    ), meter)

    return Transcript(problem_id=leaf.id, **draft.model_dump())


def transcribe_all(client, cfg: RunConfig, spec: AssignmentSpec, submission: Document,
                   mapping: StudentMapping,
                   meter: UsageMeter | None = None,
                   only_ids: set[str] | None = None) -> dict[str, Transcript]:
    """Transcribe every mapped problem (or ``only_ids``) in parallel.

    One problem's agent failure degrades to an unavailable empty transcript
    (grading then forces review, and a cached re-run retries it) instead of
    aborting the whole student.
    """
    leaves = {leaf.id: leaf for leaf in spec.leaves()}
    out: dict[str, Transcript] = {}
    with ThreadPoolExecutor(max_workers=max(1, cfg.max_workers)) as ex:
        futs = {
            pid: ex.submit(transcribe_problem, client, cfg, spec, submission,
                           leaves[pid], loc, meter)
            for pid, loc in mapping.problems.items()
            if pid in leaves and (only_ids is None or pid in only_ids)
        }
        for pid, fut in futs.items():
            try:
                out[pid] = fut.result()
            except Exception as exc:
                log.error("transcription %s FAILED (%s); keeping a flagged placeholder "
                          "and continuing — re-run to retry it", pid, exc)
                out[pid] = Transcript(
                    problem_id=pid,
                    text="",
                    confidence=0.0,
                    quality_notes="transcription unavailable because the agent failed",
                    processing_status=ProcessingStatus.failed,
                    failure=ArtifactFailure(
                        stage="transcription",
                        message=str(exc.__cause__ or exc),
                        retryable=True,
                    ),
                )
    lows = [pid for pid, t in out.items()
            if t.text and t.confidence < cfg.ocr_review_threshold]
    if lows:
        log.warning("low OCR confidence (<%.2f) for: %s", cfg.ocr_review_threshold, ", ".join(sorted(lows)))
    return out
