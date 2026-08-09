"""Pipeline orchestration: ties the stages together with resume support.

Eligible saved stage results are reused when `--force` is absent. Invalid
results are rebuilt, and failed per-problem results are retried while
successful siblings are retained. This avoids repeating completed model work
when a grading run resumes after an interruption.
"""

from __future__ import annotations

import functools
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .assignment import build_spec
from .config import RunConfig, sha256_path, slugify
from .grading import aggregate_student_grade, apply_review_thresholds, grade_student
from .ingest import Document, discover_submissions
from .llm import AGENT_FAILURE, UsageMeter, make_client
from .mapping import map_student, mapping_summary
from .models import (
    AssignmentSpec,
    Issue,
    ProcessingStatus,
    Rubric,
    SolutionsManual,
    StudentFailure,
    StudentGrade,
    StudentMapping,
    Transcript,
)
from .ocr import transcribe_all
from .report import review_queue_md, save_json, save_text, student_report_md, summary_csv, write_manifest
from .rubric import complete_rubric, generate_rubric, parse_provided_rubric, revalidate_cached_rubric, rubric_markdown
from .run_state import RunState, ensure_disjoint_output
from .solutions import (
    dependent_closure,
    generate_manual,
    parse_provided_solutions,
    solutions_markdown,
    validate_and_complete_solutions,
)

log = logging.getLogger("autograder")

M = TypeVar("M", bound=BaseModel)


class PartialGradeFailure(RuntimeError):
    def __init__(
        self,
        grades: list[StudentGrade],
        failures: list[StudentFailure],
    ) -> None:
        self.grades = grades
        self.failures = failures
        incomplete = sum(not grade.score_complete for grade in grades)
        super().__init__(
            f"grading partially failed: {incomplete} incomplete, "
            f"{len(failures)} failed students"
        )


class _Transcripts(BaseModel):
    """On-disk wrapper for a student's transcripts."""
    transcripts: dict[str, Transcript] = {}


def _files_digest(files: list[Path]) -> str:
    """Order-sensitive digest of a student's submission files (order is page order)."""
    h = hashlib.sha256()
    for f in files:
        h.update(Path(f).name.encode("utf-8"))
        h.update(bytes.fromhex(sha256_path(Path(f))))
    return h.hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _releases_documents(method):
    """Close the assignment document however the entry point exits.

    Without this, an exception raised before ``_finish`` — a failed spec pass, an
    invalid point allocation — leaks the PyMuPDF handle and any decoded page
    images. That is invisible to the CLI, which exits anyway, but the pipeline is
    documented as embeddable and must not push the cleanup onto its callers.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        finally:
            self.close()

    return wrapper


class Pipeline:
    def __init__(self, cfg: RunConfig, assignment_path: Path, out_dir: Path):
        self.cfg = cfg
        self.assignment_path = Path(assignment_path)
        ensure_disjoint_output(Path(out_dir), [self.assignment_path])
        self.run_state = RunState.open(
            Path(out_dir),
            sha256_path(self.assignment_path),
            self.cfg.cache_identity(),
        )
        self.out = self.run_state.output
        self.assignment = Document.from_path(
            self.assignment_path,
            "assignment",
            max_source_pixels=self.cfg.max_source_pixels,
        )
        self.meter = UsageMeter()
        self.issues: list[Issue] = []
        self.started = datetime.now(timezone.utc)
        self._client = None
        self._client_closed = False

    # -- plumbing -----------------------------------------------------------

    @property
    def client(self):
        """The API client, created lazily so cached-artifact runs need no key."""
        if self._client is None:
            self._client = make_client(self.cfg)
        return self._client

    def close(self) -> None:
        """Release the lazy chat client and assignment document exactly once."""
        try:
            if self._client is not None and not self._client_closed:
                self._client.close()
                self._client_closed = True
        finally:
            self.assignment.close()

    def _load_or(self, path: Path, model: type[M]) -> M | None:
        if self.cfg.force or not path.exists():
            return None
        try:
            obj = model.model_validate_json(path.read_text(encoding="utf-8"))
            log.info("reusing cached %s (use --force to rebuild)", path.name)
            return obj
        except Exception as exc:
            log.warning("could not reuse %s (%s); rebuilding", path, exc)
            return None

    def _note_unretried(self, what: str, failed: list[str]) -> None:
        msg = (f"{what} for {', '.join(failed)} failed in a previous run and were kept as "
               "flagged placeholders; set an API key and re-run to retry them")
        self.issues.append(Issue(level="warning", message=msg))
        log.warning(msg)

    def _invalidate_solution_dependents(self) -> None:
        """Remove artifacts whose contents depend on the solutions manual."""
        for path in (
            self.out / "rubric.json",
            self.out / "rubric.md",
            self.out / "summary.csv",
            self.out / "review_queue.md",
            self.out / "run_manifest.json",
        ):
            path.unlink(missing_ok=True)
        for student_dir in (self.out / "students").glob("*"):
            (student_dir / "grades.json").unlink(missing_ok=True)
            (student_dir / "report.md").unlink(missing_ok=True)

    # -- stages -------------------------------------------------------------

    def stage_spec(self) -> AssignmentSpec:
        path = self.out / "assignment_spec.json"
        spec = self._load_or(path, AssignmentSpec)
        if spec is None:
            log.info("PASS 1 — understanding the blank assignment")
            spec = build_spec(self.client, self.cfg, self.assignment, self.meter)
            save_json(path, spec)
        return spec

    def stage_solutions(self, spec: AssignmentSpec,
                        solutions_path: Path | None) -> SolutionsManual:
        ensure_disjoint_output(
            self.out,
            [Path(solutions_path)] if solutions_path is not None else [],
        )
        self.run_state.bind_input(
            "solutions",
            sha256_path(Path(solutions_path)) if solutions_path is not None else "generated",
        )
        path = self.out / "solutions_manual.json"
        manual = self._load_or(path, SolutionsManual)
        if manual is not None:
            # a previous run may have kept flagged placeholders for problems whose
            # agents failed; retry exactly those instead of trusting the placeholders
            failed = sorted(pid for pid, s in manual.solutions.items()
                            if (s.verifier_notes or "").startswith(AGENT_FAILURE))
            if failed and not self.cfg.api_key:
                self._note_unretried("solutions", failed)
            elif failed:
                log.info("retrying %d failed solution agent(s) from a previous run: %s",
                         len(failed), ", ".join(failed))
                before = manual.model_dump_json()
                affected = dependent_closure(spec, set(failed))
                regen = generate_manual(self.client, self.cfg, spec, self.assignment,
                                        only_ids=affected, known=dict(manual.solutions),
                                        meter=self.meter)
                manual.solutions.update(regen.solutions)
                if manual.model_dump_json() != before:
                    self._invalidate_solution_dependents()
                    save_json(path, manual)
        if manual is None:
            if solutions_path is not None:
                log.info("parsing provided answer key: %s", solutions_path)
                provided, issues = parse_provided_solutions(
                    self.client, self.cfg, spec, Path(solutions_path), self.assignment, self.meter)
                self.issues += issues
                manual, issues2 = validate_and_complete_solutions(
                    self.client, self.cfg, spec, self.assignment, provided, self.meter)
                self.issues += issues2
            else:
                log.info("no answer key provided — generating a verified solutions manual")
                manual = generate_manual(self.client, self.cfg, spec, self.assignment, meter=self.meter)
            save_json(path, manual)
        save_text(self.out / "solutions_manual.md", solutions_markdown(spec, manual))
        unverified = [pid for pid, s in manual.solutions.items() if not s.verified]
        if unverified:
            self.issues.append(Issue(
                level="warning",
                message="solutions not verified (grades depending on them are flagged): "
                        + ", ".join(sorted(unverified))))
        return manual

    def stage_rubric(self, spec: AssignmentSpec, manual: SolutionsManual,
                     rubric_path: Path | None, steer: str | None) -> Rubric:
        ensure_disjoint_output(
            self.out,
            [Path(rubric_path)] if rubric_path is not None else [],
        )
        self.run_state.bind_input(
            "rubric",
            sha256_path(Path(rubric_path)) if rubric_path is not None else "generated",
        )
        self.run_state.bind_input(
            "rubric_prompt", _text_digest(steer) if steer is not None else "none"
        )
        path = self.out / "rubric.json"
        rubric = self._load_or(path, Rubric)
        if rubric is not None:
            # Validate cached point invariants without an API call so grading
            # denominators stay correct on the resume path.
            before = rubric.model_dump_json()
            for i in revalidate_cached_rubric(rubric, spec):
                self.issues.append(i)
                log.log(logging.ERROR if i.level == "error" else logging.WARNING,
                        "rubric: %s", i.message)
            if rubric.model_dump_json() != before:
                log.warning("cached rubric did not meet the printed-point invariants; "
                            "re-saving the normalized version to %s", path.name)
                save_json(path, rubric)
                save_text(self.out / "rubric.md", rubric_markdown(spec, rubric))
            return rubric

        if rubric_path is not None:
            log.info("parsing provided rubric: %s", rubric_path)
            rubric, issues = parse_provided_rubric(
                self.client, self.cfg, spec, Path(rubric_path), self.meter)
            self.issues += issues
        else:
            log.info("no rubric provided — generating one"
                     + (" (steered by --rubric-prompt)" if steer else ""))
            rubric = generate_rubric(self.client, self.cfg, spec, manual,
                                     steer=steer, meter=self.meter)
        rubric, issues = complete_rubric(self.client, self.cfg, spec, manual,
                                         rubric, steer, self.meter)
        self.issues += issues
        for i in issues:
            log.log(logging.ERROR if i.level == "error" else logging.WARNING,
                    "rubric: %s", i.message)
        save_json(path, rubric)
        save_text(self.out / "rubric.md", rubric_markdown(spec, rubric))
        return rubric

    def stage_student(self, spec: AssignmentSpec, rubric: Rubric, manual: SolutionsManual,
                      student_id: str, files: list[Path]) -> StudentGrade:
        files = [Path(file) for file in files]
        ensure_disjoint_output(self.out, files)
        slug = slugify(student_id)
        self.run_state.bind_input(
            f"submission:{slug}", _files_digest(files)
        )
        sdir = self.out / "students" / slug
        submission = Document.from_paths(
            files,
            slug,
            max_source_pixels=self.cfg.max_source_pixels,
        )
        try:
            log.info("== student %s (%s)", student_id, submission.describe())

            mpath = sdir / "mapping.json"
            mapping = self._load_or(mpath, StudentMapping)
            if mapping is None:
                log.info("PASS 2 — mapping %s's work to known problems", student_id)
                mapping = map_student(self.client, self.cfg, spec, self.assignment,
                                      submission, self.meter)
                save_json(mpath, mapping)
            log.info("mapping: %s", mapping_summary(mapping))

            tpath = sdir / "transcripts.json"
            wrapped = self._load_or(tpath, _Transcripts)
            if wrapped is None:
                log.info("transcribing %s's work (parallel per problem)", student_id)
                transcripts = transcribe_all(self.client, self.cfg, spec, submission,
                                             mapping, self.meter)
                save_json(tpath, _Transcripts(transcripts=transcripts))
            else:
                transcripts = wrapped.transcripts
                failed = sorted(pid for pid, t in transcripts.items()
                                if t.processing_status is ProcessingStatus.failed)
                if failed and not self.cfg.api_key:
                    self._note_unretried(f"transcripts of {student_id}", failed)
                elif failed:
                    log.info("retrying %d failed transcription(s) for %s: %s",
                             len(failed), student_id, ", ".join(failed))
                    redo = transcribe_all(self.client, self.cfg, spec, submission,
                                          mapping, self.meter, only_ids=set(failed))
                    transcripts.update(redo)
                    save_json(tpath, _Transcripts(transcripts=transcripts))

            gpath = sdir / "grades.json"
            grade = self._load_or(gpath, StudentGrade)
            if grade is not None:
                # The review thresholds are not part of the run binding, so a
                # saved grade may have been written under different ones. Its
                # scores stand; only the review flag is re-derived.
                for problem_grade in grade.problems.values():
                    apply_review_thresholds(problem_grade, self.cfg)
                failed = sorted(pid for pid, pg in grade.problems.items()
                                if pg.processing_status is ProcessingStatus.failed)
                if failed and not self.cfg.api_key:
                    self._note_unretried(f"grades of {student_id}", failed)
                elif failed:
                    log.info("retrying %d failed grading agent(s) for %s: %s",
                             len(failed), student_id, ", ".join(failed))
                    partial = grade_student(self.client, self.cfg, spec, self.assignment,
                                            submission, student_id, rubric, manual,
                                            mapping, transcripts, self.meter,
                                            only_ids=set(failed))
                    merged = dict(grade.problems)
                    merged.update(partial.problems)
                    grade = aggregate_student_grade(student_id, mapping, transcripts, merged)
                    save_json(gpath, grade)
            if grade is None:
                log.info("grading %s (parallel per problem)", student_id)
                grade = grade_student(self.client, self.cfg, spec, self.assignment,
                                      submission, student_id, rubric, manual,
                                      mapping, transcripts, self.meter)
                save_json(gpath, grade)
            save_text(sdir / "report.md",
                      student_report_md(spec, grade, mapping, transcripts))
            n_review = sum(1 for p in grade.problems.values() if p.needs_review)
            if grade.score_complete:
                log.info("%s: %g / %g, %d problem(s) flagged for review",
                         student_id, grade.total_awarded, grade.total_possible, n_review)
            else:
                log.warning(
                    "%s: final score unavailable (%g / %g processed), "
                    "%d problem(s) flagged for review",
                    student_id, grade.processed_awarded, grade.processed_possible, n_review,
                )
            return grade
        finally:
            submission.close()

    # -- entry points -------------------------------------------------------

    @_releases_documents
    def run_inspect(self) -> AssignmentSpec:
        spec = self.stage_spec()
        self._finish(inputs={"assignment": self.assignment_path}, submissions=[],
                     run_status="complete")
        return spec

    @_releases_documents
    def run_solve(self, solutions_path: Path | None) -> SolutionsManual:
        ensure_disjoint_output(
            self.out,
            [Path(solutions_path)] if solutions_path is not None else [],
        )
        self.run_state.bind_input(
            "solutions",
            sha256_path(Path(solutions_path)) if solutions_path is not None else "generated",
        )
        spec = self.stage_spec()
        manual = self.stage_solutions(spec, solutions_path)
        self._finish(inputs={"assignment": self.assignment_path,
                             "solutions": solutions_path}, submissions=[],
                     run_status="complete")
        return manual

    @_releases_documents
    def run_rubric(self, solutions_path: Path | None, rubric_path: Path | None,
                   steer: str | None) -> Rubric:
        ensure_disjoint_output(
            self.out,
            [Path(path) for path in (solutions_path, rubric_path) if path is not None],
        )
        self.run_state.bind_input(
            "solutions",
            sha256_path(Path(solutions_path)) if solutions_path is not None else "generated",
        )
        self.run_state.bind_input(
            "rubric",
            sha256_path(Path(rubric_path)) if rubric_path is not None else "generated",
        )
        self.run_state.bind_input(
            "rubric_prompt", _text_digest(steer) if steer is not None else "none"
        )
        spec = self.stage_spec()
        manual = self.stage_solutions(spec, solutions_path)
        rubric = self.stage_rubric(spec, manual, rubric_path, steer)
        self._finish(inputs={"assignment": self.assignment_path,
                             "solutions": solutions_path, "rubric": rubric_path},
                     submissions=[], run_status="complete")
        return rubric

    @_releases_documents
    def run_grade(self, submission_paths: list[Path], solutions_path: Path | None,
                  rubric_path: Path | None, steer: str | None) -> list[StudentGrade]:
        raw_submission_paths = [Path(path) for path in submission_paths]
        ensure_disjoint_output(self.out, raw_submission_paths)
        ensure_disjoint_output(
            self.out,
            [Path(path) for path in (solutions_path, rubric_path) if path is not None],
        )
        submissions = discover_submissions(raw_submission_paths)
        if not submissions:
            raise RuntimeError("no submissions found under the given --submissions path(s)")
        for _, files in submissions:
            ensure_disjoint_output(self.out, [Path(file) for file in files])
        self.run_state.bind_input(
            "solutions",
            sha256_path(Path(solutions_path)) if solutions_path is not None else "generated",
        )
        self.run_state.bind_input(
            "rubric",
            sha256_path(Path(rubric_path)) if rubric_path is not None else "generated",
        )
        self.run_state.bind_input(
            "rubric_prompt", _text_digest(steer) if steer is not None else "none"
        )
        for student_id, files in submissions:
            self.run_state.bind_input(
                f"submission:{slugify(student_id)}", _files_digest([Path(f) for f in files])
            )
        log.info("found %d submission(s): %s", len(submissions),
                 ", ".join(sid for sid, _ in submissions))

        spec = self.stage_spec()
        manual = self.stage_solutions(spec, solutions_path)
        rubric = self.stage_rubric(spec, manual, rubric_path, steer)

        grades: list[StudentGrade] = []
        failures: list[StudentFailure] = []
        for sid, files in submissions:  # agents inside each student already run in parallel
            try:
                grades.append(self.stage_student(spec, rubric, manual, sid, files))
            except Exception as exc:
                log.error("student %s FAILED: %s", sid, exc)
                failures.append(StudentFailure(student_id=sid, message=str(exc)))
                self.issues.append(Issue(level="error", message=f"student {sid} failed: {exc}"))

        summary_csv(self.out / "summary.csv", spec, grades, failures)
        n_review = review_queue_md(self.out / "review_queue.md", spec, grades, failures)
        log.info("review queue: %d item(s)", n_review)
        if failures:
            log.error("FAILED students (re-run to resume): %s",
                      ", ".join(failure.student_id for failure in failures))
        partial = bool(failures) or any(not grade.score_complete for grade in grades)
        self._finish(inputs={"assignment": self.assignment_path,
                             "solutions": solutions_path, "rubric": rubric_path},
                     submissions=submissions,
                     run_status="partial_failure" if partial else "complete")
        if partial:
            raise PartialGradeFailure(grades, failures)
        return grades

    def _finish(self, inputs: dict, submissions: list, run_status: str) -> None:
        usage = self.meter.snapshot()
        write_manifest(self.out / "run_manifest.json", self.cfg,
                       {k: (Path(v) if v else None) for k, v in inputs.items()},
                       submissions, usage, self.started, self.issues, run_status)
        log.info(
            "OpenRouter usage: %d call(s), %d prompt tokens (+%d cache-write, %d cached), "
            "%d completion tokens (%d reasoning), $%.6f",
            usage["api_calls"], usage["prompt_tokens"], usage["cache_write_tokens"],
            usage["cached_prompt_tokens"], usage["completion_tokens"],
            usage["reasoning_tokens"], usage["cost_usd"],
        )
