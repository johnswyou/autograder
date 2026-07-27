"""Student pass, stage 1: map every known problem to the student's work.

The mapper is the heart of the "second pass". It receives the assignment's
problem inventory (the first pass) plus the student's pages, and must locate
each problem's work BY CONTENT — never by page position — because students:

* skip problems,
* insert or append their own blank/extra pages (shifting page indices),
* answer out of order or continue answers on later pages,
* mislabel work (write "2b" above work that actually answers 2c),
* occasionally write notes addressed to the grader (an injection risk).

Its output (``StudentMapping``) drives transcription and grading: each leaf
problem gets a status and a list of page regions, in reading order.
"""

from __future__ import annotations

import logging

from .assignment import spec_digest
from .config import RunConfig
from .ingest import Document
from .llm import UNTRUSTED_CONTENT_NOTE, AgentTask, UsageMeter, run_agent
from .models import AssignmentSpec, ProblemLocation, StudentMapping, WorkStatus
from .tools import Block, ToolKit, inline_pages, text_block

log = logging.getLogger("autograder")

MAPPER_SYSTEM = """You are a forensic document analyst for an automated grading system. Given \
(1) the known problem inventory of a blank assignment and (2) one student's submission, you must \
locate EVERY problem's work inside the submission.

Core rule — MATCH BY CONTENT, NOT BY PAGE POSITION:
- Students insert or append their own pages (page numbers shift), answer out of order, leave \
problems blank, cram work into margins, or continue an answer pages later ('continued on back'). \
The blank assignment's page numbers are only a hint, never proof.
- Identify work by what it actually computes/argues and by the student's labels. If the label \
and the content disagree (work labeled '2b' that clearly answers 2c), trust the CONTENT: map it \
to the problem it answers, set status='mislabeled', record label_seen and explain in note.

Procedure:
- First skim every page of the submission (you are given page images; use view_page for any not \
shown). Build a page-by-page picture: which pages mirror the original assignment, which are \
extra/inserted (list them in extra_pages), whether work appears out of order (out_of_order).
- Then, for EVERY problem id in the inventory, decide its status:
  answered (work where expected), answered_elsewhere (work found on other/extra pages), partial \
(attempt clearly incomplete), mislabeled, blank (answer space empty, nothing found anywhere), \
illegible_candidate (work exists but you doubt it can be read — too faint/blurred/messy), \
not_found (nothing attributable).
- For every problem with any work: record ALL regions containing that work, across pages, in \
reading order. Regions are [x0,y0,x1,y1] in PERCENT of the page, origin top-left. Be generous — \
include the full work area plus a small margin, not just the final answer. Split regions \
spanning pages into one region per page.
- MEASURE EVERY BBOX ON THE SUBMISSION'S OWN PAGE, never by transferring the blank assignment's \
layout. Exports (Classkick, GoodNotes, scan-to-PDF, phone photos) routinely inset the original \
page into part of a larger sheet, add headers/margins, or rescale it, so a percentage that is \
correct on the blank assignment can land a whole problem away on the student's page. Before you \
submit a region, zoom into it and confirm it actually contains THAT problem's work; if it shows a \
neighbouring problem or empty margin, correct the coordinates and check again.
- Coordinate format, every time: bbox = [x0, y0, x1, y1] as PERCENTAGES of the page (0-100), \
never pixels and never the raw numbers off a rendered image. A problem filling the middle band of \
a page is bbox=[12.5, 30.0, 88.0, 41.5]; the bottom-left quadrant is [0, 50, 50, 100]. Out-of-range \
values are rejected, so convert to percentages of the page width and height before submitting.
- Region coordinate frame: report every bbox in the page's ORIGINAL (unrotated) orientation and \
leave the region's rotate field at 0. Only if you measured a bbox from a rotated view \
(view_page/zoom with rotate=R, e.g. a sideways photo) may you instead set that region's \
rotate=R — then the bbox must be in that rotated view's coordinates. Later stages crop using \
exactly the frame you declare, so a mismatch makes them look at the wrong part of the page.
- ZOOM before judging: faint pencil, small margin notes, or a crammed correction can flip a \
'blank' to 'answered'. Never declare blank/illegible from the thumbnail alone.
- Work you cannot attribute to any known problem (doodles aside) goes in unmatched_work with a \
short description — it may be a mislabeled answer the teacher should see.
- A 'continued on page N' or arrow note means the regions continue there; follow it.

Diligence: every single problem id from the inventory MUST appear in your output exactly once. \
Do not invent problem ids.""" + UNTRUSTED_CONTENT_NOTE + """

When finished, call submit_result exactly once with the complete mapping."""


def map_student(client, cfg: RunConfig, spec: AssignmentSpec, assignment: Document,
                submission: Document, meter: UsageMeter | None = None) -> StudentMapping:
    """Run the mapper agent for one student and normalize its output."""
    toolkit = ToolKit({"assignment": assignment, "submission": submission}, cfg)

    intro = (
        f"ASSIGNMENT PROBLEM INVENTORY — {spec.title} "
        f"(original has {spec.n_pages} page(s)):\n" + spec_digest(spec, with_regions=False) +
        f"\n\nSTUDENT SUBMISSION: {submission.describe()}. "
        "Its first pages are shown below; use view_page/zoom on doc='submission' for the rest "
        "and for anything small or faint. Use doc='assignment' to compare against the original "
        "layout when helpful. Map every problem per your instructions."
    )
    content: list[Block] = [text_block(intro)]
    if submission.is_visual:
        content += inline_pages(submission, cfg.inline_page_cap, cfg.inline_page_edge)
    else:
        # text submissions (markdown/LaTeX): give the mapper the full source once
        # (inline_pages would embed the same chunks a second time)
        for page in range(1, submission.n_pages + 1):
            txt = submission.page_text(page)
            if txt:
                content.append(text_block(f"[Submission chunk {page} source]\n{txt}"))

    mapping: StudentMapping = run_agent(client, cfg, AgentTask(
        name="mapper", system=MAPPER_SYSTEM, user_content=content,
        result_model=StudentMapping, toolkit=toolkit,
        max_tokens=cfg.big_max_tokens, max_turns=cfg.max_agent_turns,
        context=submission.label,
    ), meter)

    return _normalize_mapping(mapping, spec, submission)


def _normalize_mapping(mapping: StudentMapping, spec: AssignmentSpec,
                       submission: Document) -> StudentMapping:
    """Guarantee one entry per leaf, clamp pages, drop unknown ids."""
    n = submission.n_pages
    mapping.page_count = n

    known = set(spec.leaf_ids())
    for pid in list(mapping.problems):
        if pid not in known:
            log.warning("mapper produced unknown problem id %r; dropping", pid)
            mapping.problems.pop(pid)
    for pid in spec.leaf_ids():
        if pid not in mapping.problems:
            log.warning("mapper omitted problem %s; marking mapping_error", pid)
            mapping.problems[pid] = ProblemLocation(
                status=WorkStatus.mapping_error,
                note="mapper omitted this problem",
            )

    def _ok_page(p: int) -> bool:
        return 1 <= p <= n

    for loc in mapping.problems.values():
        supplied_regions = bool(loc.regions)
        kept = [r for r in loc.regions if _ok_page(r.page)]
        if len(kept) != len(loc.regions):
            log.warning("dropping mapper region(s) with out-of-range pages")
        loc.regions = kept
        if loc.status in (WorkStatus.answered, WorkStatus.answered_elsewhere,
                          WorkStatus.partial, WorkStatus.mislabeled,
                          WorkStatus.illegible_candidate) and not loc.regions:
            # Work was claimed but no valid region remains, so we cannot infer no-work.
            log.warning("mapper status %s without valid regions; marking mapping_error", loc.status.value)
            loc.note = f"mapper marked {loc.status.value} but supplied no valid region"
            loc.status = WorkStatus.mapping_error
        elif loc.status in (WorkStatus.blank, WorkStatus.not_found) and supplied_regions:
            # Reporting no work while pointing at the answer space describes where
            # the mapper looked; it is not the contradiction it appears to be. The
            # verdict stands (grading scores it zero and routes it to review)
            # rather than becoming an unscorable mapping_error, which would
            # withhold the student's whole total over one correctly-blank item.
            detail = f"mapper marked {loc.status.value} but also supplied a work region"
            log.info("problem marked %s with a work region; keeping the verdict for review",
                     loc.status.value)
            loc.note = f"{loc.note}; {detail}" if loc.note else detail
    mapping.extra_pages = sorted({p for p in mapping.extra_pages if _ok_page(p)})
    mapping.unmatched_work = [u for u in mapping.unmatched_work if _ok_page(u.region.page)]
    return mapping


def mapping_summary(mapping: StudentMapping) -> str:
    """One-line-per-problem digest for logs and reports."""
    counts: dict[str, int] = {}
    for loc in mapping.problems.values():
        counts[loc.status.value] = counts.get(loc.status.value, 0) + 1
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    if mapping.extra_pages:
        parts.append(f"extra_pages={mapping.extra_pages}")
    if mapping.out_of_order:
        parts.append("out_of_order")
    if mapping.integrity_flags:
        parts.append(f"integrity_flags={len(mapping.integrity_flags)}")
    return ", ".join(parts)
