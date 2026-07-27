"""Solutions manual: generation, verification, and validation of provided keys.

Generation follows a generator/evaluator design:

* Problems that do not depend on earlier ones are solved IN PARALLEL, each by
  a FRESH solver agent (fresh context per problem — no cross-problem
  contamination or anchoring). Dependent problems are scheduled in
  topological levels and receive verified dependencies as official results and
  unverified drafts as advisory context.
* Every draft is reviewed by a SEPARATE evaluator agent (fresh context) that
  independently re-derives key steps and recomputes numerics with the
  ``compute`` tool. On rejection, a fresh solver regenerates with the
  evaluator's feedback, up to ``solution_max_rounds`` times. Unresolved
  solutions are kept but marked ``verified=False`` and pushed to review.

A teacher-provided answer key is parsed into the same structure, checked for
completeness against the assignment spec, and any gaps go through the same
solver/evaluator process, with the incomplete-key warning recorded — or the run
aborts under ``--strict-solutions``.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .assignment import spec_digest
from .config import RunConfig, short
from .ingest import Document
from .llm import AGENT_FAILURE, AgentTask, UsageMeter, run_agent
from .models import AssignmentSpec, Issue, ParsedSolutions, Problem, Solution, SolutionsManual, SolverDraft, Verdict
from .report import markdown_text
from .tools import Block, ToolKit, image_block, inline_pages, text_block

log = logging.getLogger("autograder")

SOLVER_SYSTEM = """You are a world-class physicist and mathematician writing the official \
solutions manual for an assignment. Solve EXACTLY the problem given — nothing more, nothing less.

Requirements:
- Work step by step: define symbols, state governing principles/theorems, derive carefully, \
track units throughout, and simplify fully. State any assumptions explicitly.
- Use the compute tool for EVERY nontrivial numeric evaluation — never do multi-step arithmetic \
in your head. Sanity-check magnitudes, signs, limiting cases, and units.
- If the problem references a figure or data you need, use the zoom/view_page tools on the \
assignment to read it precisely (axis labels, given values, angles). Never guess unreadable values.
- Use verified prerequisite results as official answers. Treat unverified prerequisite drafts as \
advisory only, and independently check them.
- Respect the requested answer form (choice letter, exact symbolic form, decimal with units, \
sketch description, proof). For diagram/sketch problems, the final_answer must DESCRIBE the \
correct diagram/curve precisely (key features, intercepts, asymptotes, arrow directions, relative \
magnitudes) so a grader can compare a student's drawing against it.
- reasoning = the full worked solution a strong instructor would publish. final_answer = the \
concise final result only.

When finished, call submit_result exactly once."""

EVALUATOR_SYSTEM = """You are an independent reviewer of a solutions manual entry, with the \
authority to reject it. You did NOT write it. Do not trust it — verify it.

Procedure:
- Re-derive the critical steps yourself. Recompute EVERY numeric result independently with the \
compute tool. Check: does it answer exactly what was asked (all required quantities, the \
requested form)? Are units consistent and present? Signs and directions right? Limiting/special \
cases sensible? Were the given values transcribed correctly from the problem (zoom into the \
assignment to confirm numbers/figures if in doubt)?
- PASS only if the solution is correct, complete, and properly presented. Minor stylistic issues \
are not grounds for rejection, but note them.
- On FAIL: list concrete issues and concise fix_suggestions (hints, not a full rewrite).

Call submit_result exactly once with your verdict."""

PARSE_SOLUTIONS_SYSTEM = """You convert a teacher-provided answer key / solutions manual into \
structured per-problem entries, matched against a known assignment problem inventory.

- Map each solution to the correct leaf problem id by CONTENT and labels (the key may order or \
label things differently than the assignment).
- Copy the reasoning/working faithfully (LaTeX for math); put the final result in final_answer. \
If the key gives only final answers, reasoning may be brief or empty.
- If an entry clearly does NOT answer the problem it is labeled as, map it to the id it actually \
answers; if it answers no known problem, describe it in unmapped_content.
- Set matches_problem=false (with mismatch_note) when content conflicts with the problem (e.g. \
solves for a different quantity or uses different given values) — this flags a stale or wrong key.
- Use zoom/view_page/read_text on either document as needed; never guess unreadable values.

Call submit_result exactly once."""


# ---------------------------------------------------------------------------
# Dependency scheduling
# ---------------------------------------------------------------------------


def dependency_levels(spec: AssignmentSpec) -> list[list[str]]:
    """Topological levels over leaf ids (Kahn). Cycles fall back to one final level."""
    leaves = spec.leaves()
    leaf_ids = {leaf.id for leaf in leaves}

    def deps_of(leaf: Problem) -> set[str]:
        out: set[str] = set()
        for d in leaf.depends_on:
            node = spec.find(d)
            if node is None:
                continue
            if node.is_leaf:
                out.add(node.id)
            else:  # depending on a container means depending on its leaves
                out.update(c.id for c in node.walk() if c.is_leaf and c.id != leaf.id)
        return out & leaf_ids - {leaf.id}

    document_order = {leaf.id: index for index, leaf in enumerate(leaves)}
    remaining = {leaf.id: deps_of(leaf) for leaf in leaves}
    levels: list[list[str]] = []
    while remaining:
        ready = sorted([pid for pid, deps in remaining.items() if not deps],
                       key=document_order.__getitem__)
        if not ready:  # cycle
            log.warning("dependency cycle among %s; solving them in one final level", sorted(remaining))
            levels.append(sorted(remaining))
            break
        levels.append(ready)
        for pid in ready:
            remaining.pop(pid)
        for deps in remaining.values():
            deps.difference_update(ready)
    return levels


# ---------------------------------------------------------------------------
# Problem context shared by solver / evaluator / grader
# ---------------------------------------------------------------------------


def problem_context_blocks(spec: AssignmentSpec, assignment: Document, leaf: Problem,
                           cfg: RunConfig, dep_solutions: dict[str, Solution]) -> list[Block]:
    blocks: list[Block] = []
    head = [f"ASSIGNMENT: {spec.title}"]
    if spec.general_instructions:
        head.append(f"General instructions: {spec.general_instructions}")
    head.append(f"\nPROBLEM {leaf.id} ({leaf.label}) — type: {leaf.type.value}, "
                f"points: {leaf.points if leaf.points is not None else 'n/a'}")
    if leaf.answer_format:
        head.append(f"Expected answer form: {leaf.answer_format}")
    if leaf.choices:
        head.append("Choices: " + "; ".join(f"({chr(65 + i)}) {c}" for i, c in enumerate(leaf.choices)))
    head.append("\nFull problem statement (stem chain, outermost first):")
    head.append(spec.stem_text(leaf.id))
    blocks.append(text_block("\n".join(head)))

    verified_dependencies = sorted(
        (pid, sol) for pid, sol in dep_solutions.items() if sol.verified
    )
    unverified_dependencies = sorted(
        (pid, sol) for pid, sol in dep_solutions.items() if not sol.verified
    )
    if verified_dependencies:
        dep_txt = ["\nOFFICIAL RESULTS OF VERIFIED PREREQUISITE PARTS (use these):"]
        for pid, sol in verified_dependencies:
            dep_txt.append(f"- {pid}: {sol.final_answer}" + (f" [{sol.method_summary}]" if sol.method_summary else ""))
        blocks.append(text_block("\n".join(dep_txt)))
    if unverified_dependencies:
        dep_txt = ["\nUNVERIFIED PREREQUISITE DRAFTS (advisory only; independently check these):"]
        for pid, sol in unverified_dependencies:
            dep_txt.append(f"- {pid}: {sol.final_answer}" + (f" [{sol.method_summary}]" if sol.method_summary else ""))
        blocks.append(text_block("\n".join(dep_txt)))

    if assignment.is_visual:
        for fig in leaf.figure_refs[:4]:
            try:
                jpg = assignment.render_region(fig.page, fig.bbox, cfg.zoom_target_edge,
                                               rotate=fig.rotate,
                                               max_upscale=cfg.max_upscale, max_pixels=cfg.max_pixels)
                blocks.append(text_block(f"[Referenced figure — assignment page {fig.page}]"))
                blocks.append(image_block(jpg))
            except Exception as exc:
                log.warning("could not render figure for %s: %s", leaf.id, exc)
        for page in leaf.pages[:2]:
            try:
                jpg = assignment.render_page(page, cfg.inline_page_edge, max_pixels=cfg.max_pixels)
                blocks.append(text_block(f"[Assignment page {page} (context)]"))
                blocks.append(image_block(jpg))
            except Exception as exc:
                log.warning("could not render page %s for %s: %s", page, leaf.id, exc)
    else:
        for page in leaf.pages[:3]:
            txt = assignment.page_text(page)
            if txt:
                blocks.append(text_block(f"[Assignment page {page} source]\n{txt}"))
    return blocks


# ---------------------------------------------------------------------------
# Generator / evaluator loop for one problem
# ---------------------------------------------------------------------------


def _unverified_dependency_blockers(dep_solutions: dict[str, Solution]) -> list[str]:
    blockers: set[str] = set()
    for pid, solution in dep_solutions.items():
        if not solution.verified:
            blockers.add(pid)
        blockers.update(solution.unverified_dependencies)
    return sorted(blockers)


def _with_dependency_blockers(notes: str | None, blockers: list[str]) -> str | None:
    if not blockers:
        return notes
    explanation = "UNVERIFIED PREREQUISITE DEPENDENCIES: " + ", ".join(blockers)
    return f"{notes}\n{explanation}" if notes else explanation


def solve_problem(client, cfg: RunConfig, spec: AssignmentSpec, assignment: Document,
                  leaf: Problem, dep_solutions: dict[str, Solution],
                  meter: UsageMeter | None) -> Solution:
    toolkit = ToolKit({"assignment": assignment}, cfg)
    base_context = problem_context_blocks(spec, assignment, leaf, cfg, dep_solutions)
    blockers = _unverified_dependency_blockers(dep_solutions)

    feedback: Verdict | None = None
    draft: SolverDraft | None = None
    rounds = 0
    for attempt in range(cfg.solution_max_rounds + 1):
        rounds = attempt + 1
        solver_content = list(base_context)
        solver_content.append(text_block("\nSolve this problem now per your instructions."))
        if feedback is not None and draft is not None:
            solver_content.append(text_block(
                "\nA previous draft was REJECTED by an independent reviewer. Produce a corrected, "
                "fully reworked solution.\n"
                f"Previous final answer: {draft.final_answer}\n"
                "Reviewer issues:\n- " + "\n- ".join(feedback.issues or ["(none listed)"]) +
                ("\nReviewer suggestions:\n- " + "\n- ".join(feedback.fix_suggestions)
                 if feedback.fix_suggestions else "")
            ))
        draft = run_agent(client, cfg, AgentTask(
            name="solver", system=SOLVER_SYSTEM, user_content=solver_content,
            result_model=SolverDraft, toolkit=toolkit, max_tokens=cfg.big_max_tokens,
            max_turns=cfg.max_agent_turns, context=leaf.id,
        ), meter)

        eval_content = list(base_context)
        eval_content.append(text_block(
            "\nCANDIDATE SOLUTION TO REVIEW\n"
            f"Final answer: {draft.final_answer}\n"
            + (f"Method: {draft.method_summary}\n" if draft.method_summary else "")
            + (f"Assumptions: {'; '.join(draft.assumptions)}\n" if draft.assumptions else "")
            + f"Worked solution:\n{draft.reasoning}\n\nVerify it now per your instructions."
        ))
        try:
            feedback = run_agent(client, cfg, AgentTask(
                name="evaluator", system=EVALUATOR_SYSTEM, user_content=eval_content,
                result_model=Verdict, toolkit=toolkit, max_tokens=cfg.max_tokens,
                max_turns=cfg.max_agent_turns, context=leaf.id,
            ), meter)
        except Exception as exc:
            # the draft exists; losing the evaluator must not lose the problem
            log.error("evaluator for %s failed (%s); keeping draft unverified", leaf.id, exc)
            return Solution(
                problem_id=leaf.id, reasoning=draft.reasoning, final_answer=draft.final_answer,
                method_summary=draft.method_summary, assumptions=draft.assumptions,
                verified=False, unverified_dependencies=blockers,
                verifier_notes=_with_dependency_blockers(
                    f"{AGENT_FAILURE} evaluator agent failed: {exc}", blockers
                ),
                provenance="generated", rounds=rounds,
            )

        if feedback.passed:
            if blockers:
                log.warning("solution %s accepted by evaluator but blocked by unverified prerequisites: %s",
                            leaf.id, ", ".join(blockers))
            else:
                log.info("solution %s verified (round %d)", leaf.id, rounds)
            return Solution(
                problem_id=leaf.id, reasoning=draft.reasoning, final_answer=draft.final_answer,
                method_summary=draft.method_summary, assumptions=draft.assumptions,
                verified=not blockers, unverified_dependencies=blockers,
                verifier_notes=_with_dependency_blockers("; ".join(feedback.issues) or None, blockers),
                provenance="generated", rounds=rounds,
            )
        log.warning("solution %s rejected on round %d: %s", leaf.id, rounds, short("; ".join(feedback.issues), 220))

        if attempt == cfg.solution_max_rounds:
            # Last round, still rejected. Returning here rather than after the
            # loop keeps ``draft`` and ``feedback`` in scope where they are
            # provably assigned; the post-loop path could only be reached with
            # both still None.
            log.error("solution %s NOT verified after %d round(s); keeping last draft flagged for review",
                      leaf.id, rounds)
            return Solution(
                problem_id=leaf.id, reasoning=draft.reasoning, final_answer=draft.final_answer,
                method_summary=draft.method_summary, assumptions=draft.assumptions,
                verified=False, unverified_dependencies=blockers,
                verifier_notes=_with_dependency_blockers(
                    "UNRESOLVED after max rounds: " + "; ".join(feedback.issues or []), blockers
                ),
                provenance="generated", rounds=rounds,
            )

    # ``solution_max_rounds`` is validated as >= 0, so the loop above runs at
    # least once and always returns from inside it.
    raise AssertionError(f"solve_problem ran no rounds for {leaf.id}")


def generate_manual(client, cfg: RunConfig, spec: AssignmentSpec, assignment: Document,
                    only_ids: set[str] | None = None,
                    known: dict[str, Solution] | None = None,
                    meter: UsageMeter | None = None) -> SolutionsManual:
    """Generate solutions for all leaves (or ``only_ids``).

    ``known`` supplies a provided answer key or cached manual during a retry,
    so dependent problems receive their prerequisites' results even when those
    prerequisites are not being generated in this call.

    One problem's agent failure never aborts the stage: the failed problem is
    kept as an unverified placeholder marked with ``AGENT_FAILURE`` (retried on
    the next cached run) and everything else proceeds.
    """
    manual = SolutionsManual(assignment_title=spec.title)
    known = known or {}
    levels = dependency_levels(spec)
    leaves = {leaf.id: leaf for leaf in spec.leaves()}
    for li, level in enumerate(levels, 1):
        todo = [pid for pid in level if only_ids is None or pid in only_ids]
        if not todo:
            continue
        log.info("solving level %d/%d: %s (parallel x%d, fresh agents)",
                 li, len(levels), ", ".join(todo), min(cfg.max_workers, len(todo)))
        with ThreadPoolExecutor(max_workers=max(1, cfg.max_workers)) as ex:
            futures = {}
            for pid in todo:
                leaf = leaves[pid]
                pool = {**known, **manual.solutions}
                deps = {d: pool[d] for d in _leaf_deps(spec, leaf) if d in pool}
                blockers = _unverified_dependency_blockers(deps)
                futures[pid] = (
                    ex.submit(solve_problem, client, cfg, spec, assignment, leaf, deps, meter),
                    blockers,
                )
            for pid, (fut, blockers) in futures.items():
                try:
                    manual.solutions[pid] = fut.result()
                except Exception as exc:
                    log.error("solution %s FAILED (%s); keeping a flagged placeholder "
                              "and continuing — re-run to retry it", pid, exc)
                    manual.solutions[pid] = Solution(
                        problem_id=pid,
                        verified=False,
                        unverified_dependencies=blockers,
                        verifier_notes=_with_dependency_blockers(
                            f"{AGENT_FAILURE} solver agent failed: {exc}", blockers
                        ),
                        provenance="generated",
                    )
    return manual


def _leaf_deps(spec: AssignmentSpec, leaf: Problem) -> list[str]:
    out: list[str] = []
    for d in leaf.depends_on:
        node = spec.find(d)
        if node is None:
            continue
        out.extend([c.id for c in node.walk() if c.is_leaf and c.id != leaf.id])
    return list(dict.fromkeys(out))  # preserve order, dedupe


def dependent_closure(spec: AssignmentSpec, seeds: set[str]) -> set[str]:
    """Return seeds plus every leaf that transitively depends on them."""
    closure = set(seeds)
    leaves = spec.leaves()
    while True:
        additions = {
            leaf.id
            for leaf in leaves
            if leaf.id not in closure and set(_leaf_deps(spec, leaf)) & closure
        }
        if not additions:
            return closure
        closure.update(additions)


def _propagate_dependency_trust(spec: AssignmentSpec, manual: SolutionsManual) -> None:
    """Mark every dependent entry unverified when a prerequisite is unverified."""
    leaves = {leaf.id: leaf for leaf in spec.leaves()}
    for level in dependency_levels(spec):
        for pid in level:
            solution = manual.solutions.get(pid)
            if solution is None:
                continue
            blockers = _unverified_dependency_blockers({
                dep_id: manual.solutions[dep_id]
                for dep_id in _leaf_deps(spec, leaves[pid])
                if dep_id in manual.solutions
            })
            if blockers:
                solution.verified = False
                solution.unverified_dependencies = blockers
                solution.verifier_notes = _with_dependency_blockers(
                    solution.verifier_notes, blockers
                )


# ---------------------------------------------------------------------------
# Provided answer key: parse + validate + complete
# ---------------------------------------------------------------------------


def parse_provided_solutions(client, cfg: RunConfig, spec: AssignmentSpec,
                             key_path: Path, assignment: Document,
                             meter: UsageMeter | None) -> tuple[dict[str, Solution], list[Issue]]:
    issues: list[Issue] = []
    if key_path.suffix.lower() == ".json":
        data = json.loads(key_path.read_text(encoding="utf-8"))
        sols: dict[str, Solution] = {}
        raw = data.get("solutions", data) if isinstance(data, dict) else {}
        for pid, entry in raw.items():
            if isinstance(entry, dict):
                sols[pid] = Solution(
                    problem_id=pid, reasoning=str(entry.get("reasoning", "")),
                    final_answer=str(entry.get("final_answer", entry.get("answer", ""))),
                    verified=True, provenance="provided",
                )
            else:
                sols[pid] = Solution(problem_id=pid, final_answer=str(entry), verified=True, provenance="provided")
        return sols, issues

    key_doc = Document.from_path(
        key_path,
        "answer_key",
        max_source_pixels=cfg.max_source_pixels,
    )
    toolkit = ToolKit({"answer_key": key_doc, "assignment": assignment}, cfg)
    content: list[Block] = [text_block(
        "Known assignment problem inventory (gradable leaves):\n" + spec_digest(spec) +
        f"\n\nBelow is the teacher-provided answer key ({key_doc.describe()}). Convert it to "
        "structured entries per your instructions, inspecting every page."
    )]
    content += inline_pages(key_doc, cfg.inline_page_cap, cfg.inline_page_edge)
    parsed: ParsedSolutions = run_agent(client, cfg, AgentTask(
        name="key_parser", system=PARSE_SOLUTIONS_SYSTEM, user_content=content,
        result_model=ParsedSolutions, toolkit=toolkit, max_tokens=cfg.big_max_tokens,
        max_turns=cfg.max_agent_turns,
    ), meter)
    key_doc.close()

    sols = {}
    known = set(spec.leaf_ids())
    for e in parsed.entries:
        if e.problem_id not in known:
            issues.append(Issue(level="warning",
                                message=f"answer key entry for unknown problem id '{e.problem_id}' ignored"))
            continue
        if not e.matches_problem:
            issues.append(Issue(level="warning",
                                message=f"answer key entry for {e.problem_id} does not match "
                                        f"the problem: {e.mismatch_note}"))
        sols[e.problem_id] = Solution(
            problem_id=e.problem_id, reasoning=e.reasoning, final_answer=e.final_answer,
            verified=e.matches_problem, provenance="provided" if e.matches_problem else "provided_unverified",
            verifier_notes=e.mismatch_note,
        )
    for desc in parsed.unmapped_content:
        issues.append(Issue(level="warning", message=f"answer key content not mapped to any problem: {desc}"))
    return sols, issues


def validate_and_complete_solutions(client, cfg: RunConfig, spec: AssignmentSpec, assignment: Document,
                                    provided: dict[str, Solution],
                                    meter: UsageMeter | None) -> tuple[SolutionsManual, list[Issue]]:
    issues: list[Issue] = []
    leaf_ids = spec.leaf_ids()
    missing = [pid for pid in leaf_ids if pid not in provided or not provided[pid].final_answer.strip()]
    extra = [pid for pid in provided if pid not in leaf_ids]
    for pid in extra:
        issues.append(Issue(level="warning", message=f"answer key covers unknown problem '{pid}' (ignored)"))
    if missing:
        msg = f"answer key is INCOMPLETE — missing solutions for: {', '.join(missing)}"
        if cfg.strict_solutions:
            issues.append(Issue(level="error", message=msg))
            raise RuntimeError(msg + " (--strict-solutions)")
        issues.append(Issue(level="warning", message=msg + " — generating the missing ones (flagged)"))

    manual = SolutionsManual(assignment_title=spec.title)
    for pid in leaf_ids:
        if pid in provided and provided[pid].final_answer.strip():
            manual.solutions[pid] = provided[pid]
    if missing:
        # pass the provided entries as known solutions so a generated gap that
        # depends on a provided part still receives its prerequisite's result
        generated = generate_manual(client, cfg, spec, assignment, only_ids=set(missing),
                                    known=dict(manual.solutions), meter=meter)
        manual.solutions.update(generated.solutions)

    if cfg.verify_provided_solutions:
        leaves = {leaf.id: leaf for leaf in spec.leaves()}
        to_verify = [pid for pid in leaf_ids if manual.solutions[pid].provenance.startswith("provided")]
        if to_verify:
            log.info("verifying %d provided solution(s) with evaluator agents", len(to_verify))
        with ThreadPoolExecutor(max_workers=max(1, cfg.max_workers)) as ex:
            futs = {}
            for pid in to_verify:
                sol = manual.solutions[pid]
                leaf = leaves[pid]
                deps = {d: manual.solutions[d] for d in _leaf_deps(spec, leaf) if d in manual.solutions}
                content = problem_context_blocks(spec, assignment, leaf, cfg, deps)
                content.append(text_block(
                    "\nCANDIDATE SOLUTION TO REVIEW (from the teacher's answer key)\n"
                    f"Final answer: {sol.final_answer}\nWorked solution:\n{sol.reasoning or '(final answer only)'}\n"
                    "\nVerify it per your instructions."
                ))
                futs[pid] = ex.submit(run_agent, client, cfg, AgentTask(
                    name="evaluator", system=EVALUATOR_SYSTEM, user_content=content,
                    result_model=Verdict, toolkit=ToolKit({"assignment": assignment}, cfg),
                    max_tokens=cfg.max_tokens, max_turns=cfg.max_agent_turns, context=pid,
                ), meter)
            for pid, fut in futs.items():
                try:
                    verdict: Verdict = fut.result()
                except Exception as exc:
                    # verification infrastructure failed — the provided entry keeps
                    # its trust level, but record that it was not actually checked
                    log.error("verification of provided solution %s failed: %s", pid, exc)
                    issues.append(Issue(level="warning",
                                        message=f"could not verify provided solution for {pid} "
                                                f"(evaluator agent failed: {exc})"))
                    continue
                if not verdict.passed:
                    issues.append(Issue(level="warning",
                                        message=f"provided solution for {pid} failed verification: "
                                                f"{'; '.join(verdict.issues)}"))
                    manual.solutions[pid].verified = False
                    manual.solutions[pid].verifier_notes = "; ".join(verdict.issues)
    _propagate_dependency_trust(spec, manual)
    return manual, issues


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def solutions_markdown(spec: AssignmentSpec, manual: SolutionsManual) -> str:
    lines = [f"# Solutions manual — {markdown_text(spec.title, single_line=True)}", ""]
    for leaf in spec.leaves():
        sol = manual.get(leaf.id)
        lines.append(
            f"## {markdown_text(leaf.id, single_line=True)} "
            f"({markdown_text(leaf.label, single_line=True)})"
        )
        if sol is None:
            lines.append("*MISSING SOLUTION*\n")
            continue
        badge = "verified" if sol.verified else "UNVERIFIED — human review recommended"
        lines.append(
            f"*{markdown_text(sol.provenance, single_line=True)}, {badge}, "
            f"rounds: {sol.rounds}*"
        )
        if sol.assumptions:
            lines.append("Assumptions: " + "; ".join(
                markdown_text(assumption, single_line=True) for assumption in sol.assumptions
            ))
        if sol.reasoning.strip():
            lines.append("")
            lines.append(markdown_text(sol.reasoning.strip()))
        lines.append("")
        lines.append(f"**Final answer:** {markdown_text(sol.final_answer)}")
        if sol.verifier_notes:
            lines.append("")
            lines.append("> Reviewer notes:")
            lines.extend("> " + line for line in markdown_text(sol.verifier_notes).splitlines())
        lines.append("")
    return "\n".join(lines)
