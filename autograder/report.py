"""Report writers: per-student reports, class summary CSV, review queue, manifest.

Every artifact is plain JSON/markdown/CSV on disk so the teacher can inspect,
diff, and re-run stages. Nothing here calls the API.
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import platform
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from . import __version__
from .config import RunConfig, sha256_path
from .models import (
    AssignmentSpec,
    Issue,
    ProcessingStatus,
    StudentFailure,
    StudentGrade,
    StudentMapping,
    Transcript,
    WorkStatus,
)
from .run_state import atomic_write_text

log = logging.getLogger("autograder")

_MARKDOWN_SYNTAX_PUNCTUATION = frozenset(r"\\!#()*+-.=>[]_`|~")


def markdown_text(value: object, *, single_line: bool = False) -> str:
    """Render untrusted data as inert Markdown text.

    NUL is rewritten to a visible ``\\x00`` for the same reason ``csv_text``
    does it: a single raw NUL makes the whole Markdown file read as binary, so
    a student report becomes unviewable and undiffable in a text workflow.
    """
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if single_line:
        text = " ".join(text.split())
    text = text.replace("\x00", r"\x00")
    # quote=False on purpose: `"` and `'` are not Markdown syntax, and escaping
    # them to numeric character references (`&#x27;`) collides with the `#`
    # backslash-escape below, which renders as the literal text `&#x27;` instead
    # of the character. `<`, `>` and `&` are still neutralized.
    text = html.escape(text, quote=False)
    return "".join(
        f"\\{character}" if character in _MARKDOWN_SYNTAX_PUNCTUATION else character
        for character in text
    )


def markdown_table_text(value: object) -> str:
    """Render inert single-line text inside a Markdown table cell."""
    return markdown_text(value, single_line=True)


def csv_text(value: object) -> str:
    """Neutralize spreadsheet formulas in text-valued CSV cells."""
    text = str(value)
    formula_capable = False
    for character in text:
        if character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F:
            continue
        formula_capable = character in "=+-@"
        break
    text = text.replace("\x00", r"\x00")
    return "'" + text if formula_capable else text


def save_json(path: Path, model: BaseModel) -> None:
    atomic_write_text(path, model.model_dump_json(indent=2))
    log.debug("wrote %s", path)


def save_text(path: Path, text: str) -> None:
    atomic_write_text(path, text)
    log.debug("wrote %s", path)


# ---------------------------------------------------------------------------
# Per-student report
# ---------------------------------------------------------------------------


def student_report_md(spec: AssignmentSpec, grade: StudentGrade, mapping: StudentMapping,
                      transcripts: dict[str, Transcript]) -> str:
    lines = [
        f"# Grade report — {markdown_text(grade.student_id, single_line=True)}",
        "",
        f"**Assignment:** {markdown_text(spec.title, single_line=True)}",
    ]
    # StudentGrade validates that these two agree; testing the value directly
    # is what makes the division below safe.
    if grade.total_awarded is not None:
        pct = (f" ({100.0 * grade.total_awarded / grade.total_possible:.1f}%)"
               if grade.total_possible else "")
        lines.append(f"**Score:** {grade.total_awarded:g} / {grade.total_possible:g}{pct}")
    else:
        lines.append("**Score:** Final score unavailable")
        lines.append(f"Processed subtotal: {grade.processed_awarded:g} / "
                     f"{grade.processed_possible:g}")
    if grade.ocr_confidence_mean is not None:
        lines.append(f"**Legibility (OCR confidence):** mean {grade.ocr_confidence_mean:.2f}, "
                     f"min {grade.ocr_confidence_min:.2f}")
    review = sorted(pid for pid, g in grade.problems.items() if g.needs_review)
    if review:
        lines.append("**Needs human review:** " + ", ".join(
            markdown_text(pid, single_line=True) for pid in review
        ))
    if grade.flags:
        lines.append("")
        lines.append("**Flags:**")
        for f in grade.flags:
            lines.append(f"- {markdown_text(f, single_line=True)}")
    lines.append("")

    for leaf in spec.leaves():
        g = grade.problems.get(leaf.id)
        if g is None:
            continue
        awarded = "unavailable" if g.awarded is None else f"{g.awarded:g}"
        lines.append(
            f"## {markdown_text(leaf.id, single_line=True)} "
            f"({markdown_text(leaf.label, single_line=True)}) — {awarded} / {g.possible:g}"
        )
        meta = [f"status: {g.status.value}"]
        if g.processing_status is ProcessingStatus.failed and g.failure is not None:
            meta.append(
                "FAILED — "
                f"{markdown_text(g.failure.stage, single_line=True)}: "
                f"{markdown_text(g.failure.message, single_line=True)}"
            )
        if g.ocr_confidence is not None and g.status not in (WorkStatus.blank, WorkStatus.not_found):
            meta.append(f"OCR {g.ocr_confidence:.2f}")
        if g.needs_review:
            meta.append(
                "NEEDS REVIEW — "
                f"{markdown_text(g.review_reason or 'see criteria', single_line=True)}"
            )
        if g.location_note:
            meta.append(markdown_text(g.location_note, single_line=True))
        lines.append(f"*{'; '.join(meta)}*")
        lines.append("")
        for cs in g.criteria:
            lines.append(
                f"- {markdown_text(cs.criterion_id, single_line=True)}: "
                f"**{cs.awarded:g} / {cs.possible:g}** — "
                f"{markdown_text(cs.justification, single_line=True)}"
            )
        if g.feedback:
            lines.append("")
            lines.extend("> " + ln for ln in markdown_text(g.feedback).splitlines())
        t = transcripts.get(leaf.id)
        if t is not None and t.text.strip():
            lines.append("")
            lines.append("<details><summary>Transcript of student's work</summary>")
            lines.append("")
            lines.append(markdown_text(t.text.strip()))
            lines.append("")
            lines.append("</details>")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Class-level outputs
# ---------------------------------------------------------------------------


def summary_csv(
    path: Path,
    spec: AssignmentSpec,
    grades: list[StudentGrade],
    failures: list[StudentFailure] | None = None,
) -> None:
    leaf_ids = spec.leaf_ids()
    with io.StringIO(newline="") as f:
        w = csv.writer(f)
        w.writerow(["student_id", "total_awarded", "total_possible", "percent",
                    *(csv_text(pid) for pid in leaf_ids), "n_needs_review", "ocr_min",
                    "flags", "run_status", "failure"])
        for g in sorted(grades, key=lambda s: s.student_id):
            pct = (round(100.0 * g.total_awarded / g.total_possible, 2)
                   if g.total_awarded is not None and g.total_possible else "")
            row = [csv_text(g.student_id), g.total_awarded if g.score_complete else "",
                   g.total_possible, pct]
            for pid in leaf_ids:
                pg = g.problems.get(pid)
                row.append(pg.awarded if pg is not None and pg.awarded is not None else "")
            row.append(sum(1 for p in g.problems.values() if p.needs_review))
            row.append(g.ocr_confidence_min if g.ocr_confidence_min is not None else "")
            row.append(csv_text(" | ".join(g.flags)))
            failures_text = [f"{p.failure.stage}: {p.failure.message}"
                             for p in g.problems.values() if p.failure is not None]
            row.extend([
                csv_text("complete" if g.score_complete else "incomplete"),
                csv_text(" | ".join(failures_text)),
            ])
            w.writerow(row)
        for failure in sorted(failures or [], key=lambda f: f.student_id):
            w.writerow([
                csv_text(failure.student_id), "", "", "", *("" for _ in leaf_ids),
                "", "", "", csv_text("failed"),
                csv_text(f"{failure.stage}: {failure.message}"),
            ])
        content = f.getvalue()
    atomic_write_text(path, content)
    log.info("wrote %s", path)


def review_queue_md(
    path: Path,
    spec: AssignmentSpec,
    grades: list[StudentGrade],
    failures: list[StudentFailure] | None = None,
) -> int:
    """Write the human-review queue; returns the number of items queued."""
    rows: list[tuple[str, str, str]] = []
    for g in sorted(grades, key=lambda s: s.student_id):
        for pid in spec.leaf_ids():
            pg = g.problems.get(pid)
            if pg is None:
                continue
            if pg.processing_status is ProcessingStatus.failed and pg.failure is not None:
                rows.append((g.student_id, pid, f"{pg.failure.stage}: {pg.failure.message}"))
            elif pg.needs_review:
                rows.append((g.student_id, pid, pg.review_reason or "flagged by grader"))
    rows.extend((failure.student_id, "—", f"{failure.stage}: {failure.message}")
                for failure in failures or [])
    rows.sort()
    lines = ["# Human review queue", ""]
    if not rows:
        lines.append("Nothing was flagged for review.")
    else:
        lines.append(f"{len(rows)} item(s) need a human decision. Sorted by student.")
        lines.append("")
        lines.append("| Student | Problem | Reason |")
        lines.append("|---|---|---|")
        for sid, pid, reason in rows:
            lines.append(
                f"| {markdown_table_text(sid)} | {markdown_table_text(pid)} | "
                f"{markdown_table_text(reason)} |"
            )
    save_text(path, "\n".join(lines))
    return len(rows)


def write_manifest(path: Path, cfg: RunConfig, inputs: dict[str, Path | None],
                   submissions: list[tuple[str, list[Path]]],
                   usage: dict, started: datetime,
                   issues: list[Issue], run_status: str) -> None:
    def _hash(p: Path | None):
        if p is None:
            return None
        return {"path": str(p), "sha256": sha256_path(p)}

    manifest = {
        "tool": "agentic-autograder",
        "tool_version": __version__,
        "run_status": run_status,
        "started_utc": started.isoformat(),
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "model": cfg.model,
        "config": {
            "max_workers": cfg.max_workers, "thinking": cfg.thinking, "effort": cfg.effort,
            "prompt_caching": cfg.prompt_caching, "max_tool_images": cfg.max_tool_images,
            "review_confidence": cfg.review_confidence,
            "ocr_review_threshold": cfg.ocr_review_threshold,
            "solution_max_rounds": cfg.solution_max_rounds,
            "strict_rubric": cfg.strict_rubric, "strict_solutions": cfg.strict_solutions,
            "verify_provided_solutions": cfg.verify_provided_solutions,
        },
        "inputs": {k: _hash(v) for k, v in inputs.items()},
        "submissions": [
            {"student_id": sid, "files": [_hash(f) for f in files]}
            for sid, files in submissions
        ],
        "issues": [i.model_dump() for i in issues],
        "usage": usage,
    }
    atomic_write_text(path, json.dumps(manifest, indent=2))
    log.info("wrote %s", path)
