"""Typed data models for every artifact the autograder produces.

These models serve double duty:

1. They are the on-disk artifact formats (``assignment_spec.json``,
   ``rubric.json``, ...), so every stage is resumable and inspectable.
2. Several of them are used directly as the JSON Schema of the agents'
   ``submit_result`` tool, which is how agents return structured output.
   Pydantic validation errors are fed back to the agent so it can repair
   its own output.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Assignment structure
# ---------------------------------------------------------------------------


class ProblemType(str, Enum):
    container = "container"          # has children; not directly gradable
    multiple_choice = "multiple_choice"
    true_false = "true_false"
    numeric = "numeric"              # a number (usually with units) is expected
    symbolic = "symbolic"            # an expression / formula is expected
    short_answer = "short_answer"
    free_response = "free_response"  # worked solution expected
    proof = "proof"
    derivation = "derivation"
    diagram = "diagram"              # e.g. free body diagram, circuit, ray diagram
    sketch_plot = "sketch_plot"      # sketch a function / graph
    table = "table"
    code = "code"
    other = "other"


class _Artifact(BaseModel):
    """Base for every artifact/result model.

    ``extra="forbid"`` makes a mis-shaped agent submission — most importantly one
    wrapped in a ``{"result": {...}}`` envelope, which some models emit — raise a
    ``ValidationError`` instead of silently validating as an all-defaults empty
    object. That error is fed back to the agent so the loop's schema-repair fires,
    and it makes the generated ``submit_result`` JSON schema advertise
    ``additionalProperties: false``, which discourages the envelope in the first place.
    """

    model_config = ConfigDict(extra="forbid")


class ProcessingStatus(str, Enum):
    complete = "complete"
    failed = "failed"


class ArtifactFailure(_Artifact):
    stage: str
    message: str
    retryable: bool = True


class StudentFailure(_Artifact):
    student_id: str
    stage: str = "student"
    message: str


_BBOX_SLOP_PCT = 2.0   # rounding tolerance outside 0-100 before a bbox is rejected


class Region(_Artifact):
    """A rectangular region of a page.

    ``bbox`` is ``[x0, y0, x1, y1]`` in *percent* of the page dimensions with
    the origin at the top-left corner. Percent coordinates make the region
    independent of render resolution, which is what lets agents "zoom".
    """

    page: int = Field(..., description="1-based page number")
    bbox: list[float] = Field(
        ...,
        min_length=4,
        max_length=4,
        description="[x0, y0, x1, y1] as percent (0-100) of page width/height, origin top-left",
    )
    rotate: int = Field(
        0,
        description=(
            "Clockwise view rotation (0, 90, 180, 270) whose coordinate frame the bbox uses. "
            "0 = the original page orientation. Set this only if you measured the bbox in a "
            "rotated view (view_page/zoom with the same rotate value)."
        ),
    )

    @field_validator("rotate")
    @classmethod
    def _valid_rotation(cls, v: int) -> int:
        if v not in (0, 90, 180, 270):
            raise ValueError("rotate must be 0, 90, 180, or 270")
        return v

    @field_validator("bbox")
    @classmethod
    def _ordered(cls, v: list[float]) -> list[float]:
        # A coordinate far outside 0-100 means the agent measured in some other
        # unit — pixels, usually. Clamping it produces a zero-width sliver that
        # renders blank while still looking like a valid region, and the agent is
        # never told. Rejecting sends the mistake back through submit_result's
        # schema-repair loop, where the agent can restate it in percent. Slop
        # inside the tolerance is ordinary rounding and is still clamped.
        if any(not -_BBOX_SLOP_PCT <= float(c) <= 100.0 + _BBOX_SLOP_PCT for c in v):
            raise ValueError(
                f"bbox must be in PERCENT of the page (0-100), origin top-left; got {v}. "
                "Those values are out of range — they look like pixels or another unit. "
                "Divide by the page width/height and give percentages."
            )
        x0, y0, x1, y1 = v
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        return [
            max(0.0, min(100.0, x0)),
            max(0.0, min(100.0, y0)),
            max(0.0, min(100.0, x1)),
            max(0.0, min(100.0, y1)),
        ]


class Problem(_Artifact):
    """One node of the assignment tree.

    Leaves (no children, type != container) are the gradable units. Every
    downstream artifact (solutions, rubric, mapping, grades) is keyed by
    leaf ``id``.
    """

    id: str = Field(..., description="Hierarchical id, e.g. '3', '3a', '3a.ii'. Unique within the assignment.")
    label: str = Field("", description="Label as printed, e.g. 'Problem 3', '(a)', 'ii.'")
    prompt: str = Field("", description="Full text of this node's prompt (not including ancestors).")
    type: ProblemType = ProblemType.free_response
    points: float | None = Field(None, description="Printed point value, if any. null if not printed.")
    pages: list[int] = Field(default_factory=list, description="1-based pages where this problem appears.")
    answer_region: Region | None = Field(
        None, description="Where students are expected to write the answer, if identifiable."
    )
    figure_refs: list[Region] = Field(
        default_factory=list, description="Regions of figures/diagrams/tables this problem refers to."
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Ids of earlier problems whose results this one uses (e.g. 'using part (a)').",
    )
    choices: list[str] | None = Field(None, description="For multiple choice: the options, in order.")
    answer_format: str | None = Field(
        None, description="Expected form of the answer, e.g. 'numeric with units (m/s)', 'sketch of v(t)'."
    )
    notes: str | None = None
    children: list[Problem] = Field(default_factory=list)

    @property
    def is_leaf(self) -> bool:
        return not self.children and self.type != ProblemType.container

    def walk(self) -> Iterator[Problem]:
        yield self
        for c in self.children:
            yield from c.walk()


class AssignmentSpec(_Artifact):
    """Complete structural inventory of the blank assignment."""

    title: str = "Untitled assignment"
    course: str | None = None
    total_points: float | None = Field(None, description="Printed total, or sum of leaf points if derivable.")
    n_pages: int = 0
    general_instructions: str | None = Field(
        None, description="Assignment-wide instructions (show work, sig figs policy, etc.)."
    )
    problems: list[Problem] = Field(default_factory=list)

    # -- traversal helpers --------------------------------------------------
    def walk(self) -> Iterator[Problem]:
        for p in self.problems:
            yield from p.walk()

    def leaves(self) -> list[Problem]:
        return [p for p in self.walk() if p.is_leaf]

    def leaf_ids(self) -> list[str]:
        return [p.id for p in self.leaves()]

    def find(self, pid: str) -> Problem | None:
        for p in self.walk():
            if p.id == pid:
                return p
        return None

    def stem_chain(self, pid: str) -> list[Problem]:
        """Ancestors of ``pid`` from root to the node itself (inclusive)."""

        def rec(nodes: list[Problem], chain: list[Problem]) -> list[Problem] | None:
            for n in nodes:
                here = chain + [n]
                if n.id == pid:
                    return here
                found = rec(n.children, here)
                if found:
                    return found
            return None

        return rec(self.problems, []) or []

    def stem_text(self, pid: str) -> str:
        parts = []
        for node in self.stem_chain(pid):
            head = node.label or node.id
            body = node.prompt.strip()
            parts.append(f"[{head}] {body}" if body else f"[{head}]")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Solutions manual
# ---------------------------------------------------------------------------


class SolverDraft(_Artifact):
    """What a solver agent submits for one leaf problem."""

    reasoning: str = Field(..., description="Complete worked solution: definitions, derivation, all steps, units.")
    final_answer: str = Field(..., description="The final answer, concise and unambiguous "
                                               "(boxed result, choice letter, description of required sketch, ...).")
    method_summary: str | None = Field(None, description="1-2 sentence summary of the method used.")
    assumptions: list[str] = Field(default_factory=list)


class Verdict(_Artifact):
    """What an evaluator agent submits after reviewing a draft solution."""

    passed: bool = Field(..., description="True only if the solution is correct, complete, and answers what was asked.")
    issues: list[str] = Field(default_factory=list,
                              description="Concrete problems found (wrong sign, missing case, unit error, ...).")
    fix_suggestions: list[str] = Field(default_factory=list,
                                       description="Actionable hints for regenerating; not a full solution.")
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class Solution(_Artifact):
    problem_id: str
    reasoning: str = ""
    final_answer: str = ""
    method_summary: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    verified: bool = False
    unverified_dependencies: list[str] = Field(default_factory=list)
    verifier_notes: str | None = None
    provenance: str = Field("generated", description="'generated' | 'provided' | 'provided_unverified'")
    rounds: int = Field(0, description="Number of generator/evaluator rounds consumed.")


class SolutionsManual(_Artifact):
    assignment_title: str = ""
    solutions: dict[str, Solution] = Field(default_factory=dict)

    def get(self, pid: str) -> Solution | None:
        return self.solutions.get(pid)


class ProvidedSolutionEntry(_Artifact):
    problem_id: str = Field(..., description="Leaf problem id from the assignment spec this solution corresponds to.")
    reasoning: str = ""
    final_answer: str = ""
    matches_problem: bool = Field(True, description="False if the content does not actually answer this problem.")
    mismatch_note: str | None = None


class ParsedSolutions(_Artifact):
    """Structured form of a teacher-supplied answer key / solutions manual."""

    entries: list[ProvidedSolutionEntry] = Field(default_factory=list)
    unmapped_content: list[str] = Field(
        default_factory=list,
        description="Brief descriptions of key content that could not be mapped to any known problem id.",
    )


# ---------------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------------


class Criterion(_Artifact):
    id: str = Field(..., description="Unique criterion id, e.g. '3a.c1'.")
    description: str = Field(..., description="Observable, objective condition for awarding these points.")
    points: float = Field(..., ge=0.0)


class RubricProblem(_Artifact):
    problem_id: str
    points: float = Field(..., ge=0.0, description="Total points for this problem; criteria must sum to this.")
    criteria: list[Criterion] = Field(default_factory=list)
    grading_notes: str | None = Field(None, description="Tolerances, alternative methods, common-error guidance.")


class Rubric(_Artifact):
    title: str = ""
    total_points: float | None = None
    problems: list[RubricProblem] = Field(default_factory=list)

    def for_problem(self, pid: str) -> RubricProblem | None:
        for rp in self.problems:
            if rp.problem_id == pid:
                return rp
        return None

    def ids(self) -> list[str]:
        return [rp.problem_id for rp in self.problems]


class ParsedRubric(_Artifact):
    rubric: Rubric
    unmapped_content: list[str] = Field(default_factory=list)


class Issue(_Artifact):
    level: str = Field(..., description="'error' or 'warning'")
    message: str


# ---------------------------------------------------------------------------
# Student pass: mapping + transcription
# ---------------------------------------------------------------------------


class WorkStatus(str, Enum):
    answered = "answered"                    # found where expected
    answered_elsewhere = "answered_elsewhere"  # found, but on extra/other pages
    partial = "partial"                      # attempt started but clearly incomplete
    mislabeled = "mislabeled"                # student labeled it as a different problem; matched by content
    blank = "blank"                          # answer space left empty; no work found anywhere
    illegible_candidate = "illegible_candidate"  # work exists but mapper doubts it can be read
    not_found = "not_found"                  # no work located for this problem
    mapping_error = "mapping_error"          # mapper could not reliably locate this problem's work


class ProblemLocation(_Artifact):
    status: WorkStatus = WorkStatus.not_found
    regions: list[Region] = Field(
        default_factory=list,
        description="All regions (possibly across multiple pages) containing this problem's work, in reading order.",
    )
    label_seen: str | None = Field(None, description="The label the student actually wrote, if any.")
    note: str | None = Field(None, description="e.g. 'continued on appended page 7', 'labeled 2b but content is 2c'.")


class UnmatchedWork(_Artifact):
    region: Region
    description: str = ""


class StudentMapping(_Artifact):
    page_count: int = 0
    problems: dict[str, ProblemLocation] = Field(
        default_factory=dict, description="One entry per leaf problem id from the assignment spec."
    )
    extra_pages: list[int] = Field(
        default_factory=list, description="Pages not part of the original assignment (inserted/appended)."
    )
    out_of_order: bool = False
    unmatched_work: list[UnmatchedWork] = Field(
        default_factory=list, description="Work that could not be attributed to any known problem."
    )
    integrity_flags: list[str] = Field(
        default_factory=list,
        description="Suspicious content, e.g. instructions addressed to the grader/AI asking for marks.",
    )
    overall_notes: str | None = None


class TranscriptDraft(_Artifact):
    text: str = Field(..., description="Verbatim transcription; LaTeX for math; "
                                       "[illegible] / [illegible: guess?] markers; [struck: ...] for crossed-out work.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Calibrated OCR confidence for the whole transcription.")
    illegible_spans: int = Field(0, ge=0)
    quality_notes: str | None = Field(None,
                                      description="Image/handwriting quality remarks (blur, pencil, cut off, ...).")
    integrity_flags: list[str] = Field(default_factory=list)


class Transcript(TranscriptDraft):
    # ``text`` and ``confidence`` are re-declared to loosen the parent's required
    # fields: the on-disk artifact must be constructible for blank/not-found
    # problems (empty transcript), while the agent-facing TranscriptDraft keeps
    # them required so a transcriber cannot omit them.
    problem_id: str = ""
    text: str = ""
    confidence: float = 0.0
    processing_status: ProcessingStatus = ProcessingStatus.complete
    failure: ArtifactFailure | None = None

    @model_validator(mode="after")
    def _valid_processing_outcome(self) -> Transcript:
        if self.processing_status is ProcessingStatus.complete and self.failure is not None:
            raise ValueError("a complete transcript cannot have a failure")
        if self.processing_status is ProcessingStatus.failed:
            if self.failure is None:
                raise ValueError("a failed transcript must have a failure")
            if self.text or self.confidence != 0.0:
                raise ValueError("a failed transcript must have empty text and zero confidence")
        return self


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


class CriterionScore(_Artifact):
    criterion_id: str
    awarded: float = Field(..., ge=0.0)
    possible: float = Field(..., ge=0.0)
    justification: str = Field("", description="Short, evidence-based reason quoting the student's work where useful.")


class GradeDraft(_Artifact):
    criteria: list[CriterionScore] = Field(default_factory=list)
    feedback: str = Field("", description="Constructive feedback addressed to the student.")
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    needs_review: bool = False
    review_reason: str | None = None
    integrity_flags: list[str] = Field(default_factory=list)


class ProblemGrade(GradeDraft):
    problem_id: str = ""
    status: WorkStatus = WorkStatus.not_found
    awarded: float | None = 0.0
    possible: float = 0.0
    ocr_confidence: float | None = None
    location_note: str | None = None
    processing_status: ProcessingStatus = ProcessingStatus.complete
    failure: ArtifactFailure | None = None
    # The review reasons that describe this graded work itself. Held apart from
    # ``review_reason`` because the remaining reasons only compare a confidence
    # against a configured threshold, and those are recomputed on every read
    # rather than trusted from the file. See ``grading.apply_review_thresholds``.
    intrinsic_review_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_processing_outcome(self) -> ProblemGrade:
        if self.processing_status is ProcessingStatus.complete:
            if self.failure is not None or self.awarded is None:
                raise ValueError("a complete grade must have an award and no failure")
        elif self.failure is None or self.awarded is not None or self.criteria:
            raise ValueError("a failed grade must have a failure, no award, and no criteria")
        return self


class StudentGrade(_Artifact):
    student_id: str
    total_awarded: float | None = 0.0
    total_possible: float = 0.0
    processed_awarded: float = 0.0
    processed_possible: float = 0.0
    score_complete: bool = True
    ocr_confidence_mean: float | None = None
    ocr_confidence_min: float | None = None
    problems: dict[str, ProblemGrade] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _score_matches_total_availability(self) -> StudentGrade:
        if self.score_complete != (self.total_awarded is not None):
            raise ValueError("score_complete must match total_awarded availability")
        return self


Problem.model_rebuild()
AssignmentSpec.model_rebuild()
