"""Rubric: generation, parsing of teacher-provided rubrics, validation, completion.

The rubric is the contract between the solutions manual (ground truth) and the
grader agents. Generation is steerable with an instructor prompt
(``--rubric-prompt``, e.g. "weight method over arithmetic; award no points for
unjustified answers"). A provided rubric is validated for completeness against
the assignment spec: every gradable leaf must be covered, criteria must sum to
the problem's points, and totals should match printed point values. Gaps are
auto-filled (and flagged) or, under ``--strict-rubric``, abort the run.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path

from .assignment import spec_digest
from .config import RunConfig, short
from .ingest import Document
from .llm import AgentTask, UsageMeter, run_agent
from .models import AssignmentSpec, Criterion, Issue, ParsedRubric, Problem, Rubric, RubricProblem, SolutionsManual
from .report import markdown_text
from .tools import Block, ToolKit, inline_pages, text_block

log = logging.getLogger("autograder")

_POINT_TOL = 1e-6
# Stands in for the assignment itself as the outermost printed total.
_ROOT_KEY = "\x00assignment"


class PointAllocationError(ValueError):
    pass


RUBRIC_SYSTEM = """You are an experienced physics/math instructor designing a grading rubric \
that other graders (human or AI) will apply EXACTLY as written, without you present to clarify.

Requirements for every problem:
- Decompose the problem's points into 1-6 criteria. Each criterion must be OBSERVABLE and \
OBJECTIVE: a grader looking at a student's work can decide yes/no/partial without guessing \
intent. Good: 'Applies conservation of energy with both KE and PE terms (2 pt)'. Bad: \
'Understands energy (2 pt)'.
- Criterion points MUST sum exactly to the problem's total points. Use the official solution to \
decide what the key steps are; weight setup/method, execution, and final answer sensibly for the \
problem type (a multiple-choice item may be a single all-or-nothing criterion; a derivation \
should reward intermediate milestones).
- Give each criterion a unique id of the form '<problem_id>.c<N>' (e.g. '3a.c1').
- Use grading_notes for tolerances and judgment calls: numeric rounding tolerance, unit \
expectations, credit for valid alternative methods, common errors and how to score them, \
follow-through policy when an earlier part's wrong answer is reused.
- Do not invent point values: each problem's total is given to you. The rubric total must equal \
the assignment total.

When finished, call submit_result exactly once with the complete rubric covering EVERY listed \
problem."""

PARSE_RUBRIC_SYSTEM = """You convert a teacher-provided grading rubric document into a \
structured rubric, matched against a known assignment problem inventory.

- Map each rubric section to the correct leaf problem id by CONTENT and labels (the rubric may \
order or label things differently than the assignment).
- Copy criteria descriptions and point values faithfully. If the document gives only a total per \
problem with prose guidance, create a single criterion worth the total and put the guidance in \
grading_notes.
- Preserve any stated tolerances, alternative-method policies, or partial-credit rules in \
grading_notes.
- Content that maps to no known problem goes in unmapped_content (brief description).
- Use zoom/view_page/read_text as needed; never guess unreadable point values.

Call submit_result exactly once."""


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _problem_points(spec: AssignmentSpec,
                    provided: Rubric | None = None) -> dict[str, float]:
    """Resolve authoritative leaf weights without inventing an allocation."""
    leaves = spec.leaves()
    leaf_ids = {leaf.id for leaf in leaves}
    by_id: dict[str, list[RubricProblem]] = {pid: [] for pid in leaf_ids}
    if provided is not None:
        for rp in provided.problems:
            if rp.problem_id in by_id:
                by_id[rp.problem_id].append(rp)
        duplicates = [pid for pid, entries in by_id.items() if len(entries) > 1]
        if duplicates:
            raise PointAllocationError(
                f"invalid point allocation for assignment '{spec.title}'; provide exactly one "
                "teacher rubric entry per leaf with explicit leaf weights "
                f"(duplicated: {', '.join(sorted(duplicates))})"
            )

    printed = {leaf.id: float(leaf.points) for leaf in leaves if leaf.points is not None}
    if len(printed) == len(leaves):
        if provided is not None:
            for rp in provided.problems:
                if rp.problem_id in printed and abs(rp.points - printed[rp.problem_id]) > _POINT_TOL:
                    raise PointAllocationError(
                        f"provided rubric weight for leaf '{rp.problem_id}' is {rp.points:g} "
                        f"points, but its explicit leaf weight is {printed[rp.problem_id]:g}; "
                        "provide explicit leaf weights that agree with the printed value"
                    )
        _validate_point_totals(spec, printed)
        return printed

    if spec.total_points is None and all(node.points is None for node in spec.walk()):
        return {leaf.id: 1.0 for leaf in leaves}

    # Authority runs printed value, then supplied rubric entry, then derivation.
    points: dict[str, float] = {}
    for leaf in leaves:
        entries = by_id[leaf.id]
        if leaf.points is not None:
            if entries and abs(float(entries[0].points) - leaf.points) > _POINT_TOL:
                raise PointAllocationError(
                    f"provided rubric weight for leaf '{leaf.id}' is {entries[0].points:g} points, "
                    f"but its explicit leaf weight is {leaf.points:g}; provide explicit leaf "
                    "weights that agree with the printed value"
                )
            points[leaf.id] = float(leaf.points)
        elif entries:
            points[leaf.id] = float(entries[0].points)

    # Which leaves a printed total can pay for is a fact about the paper, not
    # about who supplied their weights, so it is read off the printed values
    # alone. Deciding it from whatever happened to be missing at call time made
    # the check depend on the caller: the rubric stage re-resolves the
    # allocation against the rubric it just generated, and every derived weight
    # arrives back as a supplied entry, so nothing derived, nothing was marked
    # defaulted, and the totals suppressed on the first pass were enforced on
    # the second against the very numbers the first pass had produced.
    unpayable = _derive_missing_weights(spec, leaves, dict(printed))
    _derive_missing_weights(spec, leaves, points)
    _validate_point_totals(spec, points, unpayable)
    return points


def _ancestor_chains(spec: AssignmentSpec) -> dict[str, list[Problem]]:
    """Map every node id to its ancestors, nearest first."""
    chains: dict[str, list[Problem]] = {}

    def walk(node: Problem, ancestors: list[Problem]) -> None:
        chains[node.id] = ancestors
        for child in node.children:
            walk(child, [node, *ancestors])

    for problem in spec.problems:
        walk(problem, [])
    return chains


def _derive_missing_weights(spec: AssignmentSpec, leaves: list[Problem],
                            points: dict[str, float]) -> set[str]:
    """Fill each unweighted leaf from the nearest printed parent total.

    An exam prints "15 points" beside question 22 and nothing beside 22a-22e,
    so the only honest reading of the paper is that the parts share the 15.
    ``spec.total_points`` is the same statement about the whole assignment, so
    it acts as the outermost parent.

    Leaves the enclosing total cannot pay for — because it is absent, or
    already spent by its printed siblings, as when a printed total covers one
    section of a paper and says nothing about another — fall back to a flat
    1.0. Their ids come back so the caller can skip the printed-total checks
    they have, by construction, just broken.
    """
    chains = _ancestor_chains(spec)
    groups: dict[str, list[Problem]] = {}
    governors: dict[str, Problem | None] = {}
    for leaf in leaves:
        if leaf.id in points:
            continue
        priced = next((node for node in chains[leaf.id] if node.points is not None), None)
        key = priced.id if priced is not None else _ROOT_KEY
        groups.setdefault(key, []).append(leaf)
        governors[key] = priced

    def depth(key: str) -> int:
        return -1 if key == _ROOT_KEY else len(chains[key])

    defaulted: set[str] = set()
    # Deepest first: an inner total must be spent before an outer one counts it.
    for key in sorted(groups, key=depth, reverse=True):
        members = groups[key]
        parent = governors[key]
        total = spec.total_points if parent is None else parent.points
        scope = leaves if parent is None else [n for n in parent.walk() if n.is_leaf]
        share: float | None = None
        if total is not None:
            remainder = float(total) - sum(points[n.id] for n in scope if n.id in points)
            if remainder > _POINT_TOL:
                share = remainder / len(members)
        for leaf in members:
            points[leaf.id] = 1.0 if share is None else share
            if share is None:
                defaulted.add(leaf.id)
    return defaulted


def _validate_point_totals(spec: AssignmentSpec, points: dict[str, float],
                           defaulted: set[str] | frozenset[str] = frozenset()) -> None:
    """Check resolved leaf weights against every printed aggregate total.

    A printed total is only evidence about the leaves it actually covers, so
    any total enclosing a leaf that fell back to the flat default is skipped:
    that leaf is outside the accounting the number describes.
    """

    def subtree_total(node) -> float:
        if node.is_leaf:
            return points[node.id]
        total = sum(subtree_total(child) for child in node.children)
        covers_default = any(n.id in defaulted for n in node.walk())
        if node.points is not None and not covers_default and abs(total - node.points) > _POINT_TOL:
            raise PointAllocationError(
                f"printed parent total for '{node.id}' is {node.points:g} points, but "
                f"explicit leaf weights sum to {total:g}; provide explicit leaf weights "
                "that agree with the parent total"
            )
        return total

    total = sum(subtree_total(problem) for problem in spec.problems)
    if spec.total_points is not None and not defaulted and abs(total - spec.total_points) > _POINT_TOL:
        raise PointAllocationError(
            f"printed assignment total for '{spec.title}' is {spec.total_points:g} points, "
            f"but explicit leaf weights sum to {total:g}; provide explicit leaf weights "
            "that agree with the assignment total"
        )


def check_point_allocation(spec: AssignmentSpec) -> None:
    """Raise if this spec's leaf weights cannot be resolved without a rubric.

    Callers use this to ask the question early. Generating solutions cannot
    change the answer, so a run that would fail at the rubric stage for want of
    a point allocation may as well fail before spending on them.

    Any weight that had to be derived rather than read off the paper is an
    inference, so it is announced here — before anything expensive depends
    on it — rather than left for a reader to reconstruct from the report.
    """
    points = _problem_points(spec)
    printed = {leaf.id for leaf in spec.leaves() if leaf.points is not None}
    derived = [weight for pid, weight in points.items() if pid not in printed]
    if derived:
        tally = sorted(Counter(derived).items())
        log.info(
            "derived weights for %d unpriced leaf/leaves (%s); assignment totals %g points",
            len(derived),
            ", ".join(f"{count} at {weight:g}" for weight, count in tally),
            sum(points.values()),
        )


def generate_rubric(client, cfg: RunConfig, spec: AssignmentSpec, manual: SolutionsManual,
                    steer: str | None = None,
                    only_ids: set[str] | None = None,
                    meter: UsageMeter | None = None) -> Rubric:
    """Generate rubric entries for all leaves (or ``only_ids``) in one agent pass.

    One pass (rather than per-problem agents) keeps point weighting consistent
    across the assignment; the solutions manual supplies the key steps each
    criterion should reward.
    """
    points = _problem_points(spec)
    leaves = [leaf for leaf in spec.leaves() if only_ids is None or leaf.id in only_ids]

    lines = ["PROBLEMS TO COVER (one rubric entry per problem, points fixed as given):"]
    for leaf in leaves:
        lines.append(f"\n=== problem_id={leaf.id} | {leaf.label} | type={leaf.type.value} | "
                     f"points={points[leaf.id]:g}")
        lines.append("Statement:\n" + spec.stem_text(leaf.id))
        if leaf.answer_format:
            lines.append(f"Expected answer form: {leaf.answer_format}")
        sol = manual.get(leaf.id)
        if sol is not None:
            lines.append(f"Official final answer: {sol.final_answer}")
            if sol.method_summary:
                lines.append(f"Official method: {sol.method_summary}")
            if sol.reasoning.strip():
                lines.append("Key steps (from official solution): " + short(sol.reasoning, 700))
        else:
            lines.append("(no official solution available — design criteria from the statement alone)")
    total = sum(points[leaf.id] for leaf in leaves)
    lines.append(f"\nAssignment: {spec.title}. Rubric total must equal {total:g} points.")
    if steer:
        lines.append(
            "\nINSTRUCTOR GRADING PREFERENCES — honor these when designing criteria and "
            "grading_notes (they steer weighting/policy, never the fixed point totals):\n" + steer
        )

    rubric: Rubric = run_agent(client, cfg, AgentTask(
        name="rubric", system=RUBRIC_SYSTEM, user_content=[text_block("\n".join(lines))],
        result_model=Rubric, toolkit=None, tool_names=(),
        max_tokens=cfg.big_max_tokens, max_turns=cfg.max_agent_turns,
    ), meter)

    rubric.title = rubric.title or f"Rubric — {spec.title}"
    _normalize_rubric(rubric, points, only_ids={leaf.id for leaf in leaves})
    return rubric


def _normalize_rubric(rubric: Rubric, points: dict[str, float], only_ids: set[str]) -> None:
    """Force fixed point totals, rescale criteria that don't sum, drop strays."""
    rubric.problems = [rp for rp in rubric.problems if rp.problem_id in only_ids]
    for rp in rubric.problems:
        want = points.get(rp.problem_id, rp.points)
        rp.points = want
        if not rp.criteria:
            rp.criteria = [Criterion(id=f"{rp.problem_id}.c1",
                                     description="Correct and complete response per the official solution.",
                                     points=want)]
            continue
        got = sum(c.points for c in rp.criteria)
        if abs(got - want) > _POINT_TOL and got > 0:
            log.warning("rubric %s criteria summed to %g, rescaling to %g", rp.problem_id, got, want)
            for c in rp.criteria:
                c.points = round(c.points * want / got, 4)
            drift = want - sum(c.points for c in rp.criteria)
            rp.criteria[-1].points = round(rp.criteria[-1].points + drift, 4)
        elif got <= 0 < want:
            rp.criteria = [Criterion(id=f"{rp.problem_id}.c1",
                                     description="Correct and complete response per the official solution.",
                                     points=want)]
    _dedupe_criterion_ids(rubric)
    rubric.total_points = round(sum(rp.points for rp in rubric.problems), 4)


def _dedupe_criterion_ids(rubric: Rubric) -> None:
    """Criterion ids are load-bearing at grading time (scores are matched by id);
    duplicates would silently collapse in the grader's lookup, making one of the
    criteria ungradable. Rename duplicates instead of merely warning."""
    seen: set[str] = set()
    for rp in rubric.problems:
        for i, c in enumerate(rp.criteria, 1):
            cid = re.sub(r"\s+", "", c.id) or f"{rp.problem_id}.c{i}"
            if cid in seen:
                base, n = cid, 2
                while f"{base}_{n}" in seen:
                    n += 1
                cid = f"{base}_{n}"
                log.warning("duplicate criterion id %r renamed to %r", c.id, cid)
            c.id = cid
            seen.add(cid)


# ---------------------------------------------------------------------------
# Provided rubric: parse + validate + complete
# ---------------------------------------------------------------------------


def parse_provided_rubric(client, cfg: RunConfig, spec: AssignmentSpec,
                          rubric_path: Path,
                          meter: UsageMeter | None) -> tuple[Rubric, list[Issue]]:
    issues: list[Issue] = []
    if rubric_path.suffix.lower() == ".json":
        rubric = Rubric.model_validate_json(rubric_path.read_text(encoding="utf-8"))
        return rubric, issues

    doc = Document.from_path(
        rubric_path,
        "rubric",
        max_source_pixels=cfg.max_source_pixels,
    )
    toolkit = ToolKit({"rubric": doc}, cfg)
    content: list[Block] = [text_block(
        "Known assignment problem inventory (gradable leaves):\n" + spec_digest(spec) +
        f"\n\nBelow is the teacher-provided rubric ({doc.describe()}). Convert it to a "
        "structured rubric per your instructions, inspecting every page."
    )]
    content += inline_pages(doc, cfg.inline_page_cap, cfg.inline_page_edge)
    parsed: ParsedRubric = run_agent(client, cfg, AgentTask(
        name="rubric_parser", system=PARSE_RUBRIC_SYSTEM, user_content=content,
        result_model=ParsedRubric, toolkit=toolkit,
        max_tokens=cfg.big_max_tokens, max_turns=cfg.max_agent_turns,
    ), meter)
    doc.close()
    for desc in parsed.unmapped_content:
        issues.append(Issue(level="warning", message=f"rubric content not mapped to any problem: {desc}"))
    return parsed.rubric, issues


def validate_rubric(rubric: Rubric, spec: AssignmentSpec) -> list[Issue]:
    """Completeness/consistency check of a rubric against the assignment spec."""
    issues: list[Issue] = []
    leaf_ids = spec.leaf_ids()
    have = rubric.ids()

    missing = [pid for pid in leaf_ids if pid not in have]
    if missing:
        issues.append(Issue(level="error",
                            message=f"rubric is INCOMPLETE — no entry for: {', '.join(missing)}"))
    for pid in have:
        if pid not in leaf_ids:
            issues.append(Issue(level="warning",
                                message=f"rubric covers unknown problem '{pid}' (ignored)"))
    seen_crit: set[str] = set()
    for rp in rubric.problems:
        if rp.problem_id not in leaf_ids:
            continue
        if not rp.criteria:
            issues.append(Issue(level="error", message=f"rubric for {rp.problem_id} has no criteria"))
        csum = sum(c.points for c in rp.criteria)
        if rp.criteria and abs(csum - rp.points) > _POINT_TOL:
            issues.append(Issue(level="warning",
                                message=f"rubric {rp.problem_id}: criteria sum to {csum:g} "
                                        f"but problem is worth {rp.points:g}"))
        leaf = spec.find(rp.problem_id)
        if leaf is not None and leaf.points is not None and abs(rp.points - leaf.points) > _POINT_TOL:
            issues.append(Issue(level="warning",
                                message=f"rubric {rp.problem_id}: {rp.points:g} pt differs "
                                        f"from printed {leaf.points:g} pt"))
        for c in rp.criteria:
            if c.id in seen_crit:
                issues.append(Issue(level="warning", message=f"duplicate criterion id '{c.id}'"))
            seen_crit.add(c.id)
    if spec.total_points is not None:
        rtotal = sum(rp.points for rp in rubric.problems if rp.problem_id in leaf_ids)
        if abs(rtotal - spec.total_points) > _POINT_TOL:
            issues.append(Issue(level="warning",
                                message=f"rubric total {rtotal:g} differs from assignment total {spec.total_points:g}"))
    return issues


def complete_rubric(client, cfg: RunConfig, spec: AssignmentSpec, manual: SolutionsManual,
                    rubric: Rubric, steer: str | None,
                    meter: UsageMeter | None) -> tuple[Rubric, list[Issue]]:
    """Validate a rubric; fill missing problems or abort under ``--strict-rubric``."""
    issues = validate_rubric(rubric, spec)
    points = _problem_points(spec, rubric)
    leaf_ids = spec.leaf_ids()
    missing = [pid for pid in leaf_ids if rubric.for_problem(pid) is None]

    if missing and cfg.strict_rubric:
        raise RuntimeError(
            f"rubric is incomplete (missing: {', '.join(missing)}) (--strict-rubric)")

    if missing:
        log.warning("auto-generating rubric entries for: %s", ", ".join(missing))
        gen = generate_rubric(client, cfg, spec, manual, steer=steer,
                              only_ids=set(missing), meter=meter)
        for rp in gen.problems:
            rp.grading_notes = ("[auto-generated] " + (rp.grading_notes or "")).strip()
            rubric.problems.append(rp)
        issues.append(Issue(level="warning",
                            message=f"auto-generated rubric entries (marked) for: {', '.join(missing)}"))

    # Force the printed-point invariants (fill empty criteria, rescale sums, drop
    # strays, order): a *provided* rubric otherwise reaches grading with criteria
    # that don't sum to the printed points, so `possible` would diverge from them.
    apply_point_invariants(rubric, spec, points)
    return rubric, issues


def apply_point_invariants(rubric: Rubric, spec: AssignmentSpec,
                           points: dict[str, float] | None = None) -> None:
    """Deterministically enforce the printed-point invariants on a rubric (no API).

    Covers every gradable leaf, fills empty criteria, rescales criteria to sum to the
    printed problem points, drops strays, restores spec order, and recomputes the total.
    This is what guarantees each problem's ``possible`` (the grading denominator) equals
    its printed points, whether the rubric was generated, parsed from teacher input,
    or loaded defensively from pipeline-owned resume data.
    """
    leaf_ids = spec.leaf_ids()
    if points is None:
        points = _problem_points(spec, rubric)
    have = {rp.problem_id for rp in rubric.problems}
    for pid in leaf_ids:
        if pid not in have:
            rubric.problems.append(RubricProblem(problem_id=pid, points=points[pid], criteria=[]))
    _normalize_rubric(rubric, points, set(leaf_ids))  # fill-empty + rescale + drop strays + total
    order = {pid: i for i, pid in enumerate(leaf_ids)}
    rubric.problems.sort(key=lambda rp: order.get(rp.problem_id, len(order)))
    rubric.title = rubric.title or f"Rubric — {spec.title}"


def revalidate_cached_rubric(rubric: Rubric, spec: AssignmentSpec) -> list[Issue]:
    """Validate and enforce point invariants on a rubric loaded from disk (no API).

    Cached ``rubric.json`` is pipeline-owned resume data. Defensive validation
    keeps grading denominators correct if that data is malformed before a resumed
    run skips generation. Returns discrepancies surfaced in the run issues.
    """
    issues = validate_rubric(rubric, spec)
    points = _problem_points(spec, rubric)
    apply_point_invariants(rubric, spec, points)
    return issues


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def rubric_markdown(spec: AssignmentSpec, rubric: Rubric) -> str:
    lines = [f"# {markdown_text(rubric.title or 'Rubric', single_line=True)}", ""]
    if rubric.total_points is not None:
        lines.append(f"Total: {rubric.total_points:g} points")
        lines.append("")
    for rp in rubric.problems:
        leaf = spec.find(rp.problem_id)
        label = (
            f" ({markdown_text(leaf.label, single_line=True)})"
            if leaf is not None and leaf.label else ""
        )
        lines.append(
            f"## {markdown_text(rp.problem_id, single_line=True)}{label} — {rp.points:g} pt"
        )
        for c in rp.criteria:
            lines.append(
                f"- **{markdown_text(c.id, single_line=True)}** ({c.points:g} pt): "
                f"{markdown_text(c.description)}"
            )
        if rp.grading_notes:
            lines.append("")
            lines.append("> Notes:")
            lines.extend("> " + line for line in markdown_text(rp.grading_notes).splitlines())
        lines.append("")
    return "\n".join(lines)
