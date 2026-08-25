"""Command-line interface.

    autograder inspect --assignment hw3.pdf --out runs/hw3
    autograder solve   --assignment hw3.pdf --out runs/hw3 [--solutions key.pdf]
    autograder rubric  --assignment hw3.pdf --out runs/hw3 [--rubric-prompt "..."]
    autograder grade   --assignment hw3.pdf --submissions scans/ --out runs/hw3

Requires OPENROUTER_API_KEY in the environment (or --api-key) whenever a
command needs to call the model; previously saved results can be reused
without it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import DEFAULT_MODEL, PROVIDER_SORTS, REASONING_EFFORTS, RunConfig
from .orchestrator import PartialGradeFailure, Pipeline

log = logging.getLogger("autograder")


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _unit_float(raw: str) -> float:
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return value


def _parent_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    req = p.add_argument_group("required")
    req.add_argument("--assignment", "-a", required=True, metavar="PATH",
                     help=(
                         "Blank assignment file or directory "
                         "(PDF, JPEG/PNG, Markdown, or LaTeX)"
                     ))
    req.add_argument("--out", "-o", required=True, metavar="DIR",
                     help=(
                         "Directory for generated results. Reuse it only with the "
                         "same inputs and grading settings; choose a new directory "
                         "after a change."
                     ))
    opt = p.add_argument_group("model & run options")
    opt.add_argument("--model", default=DEFAULT_MODEL,
                     help=f"OpenRouter model slug (default: {DEFAULT_MODEL})")
    opt.add_argument("--api-key", default=None,
                     help=(
                         "OpenRouter API key (default: read OPENROUTER_API_KEY from "
                         "the environment)"
                     ))
    opt.add_argument("--max-workers", type=_positive_int, default=4,
                     help="Maximum model tasks to run at once (default: 4)")
    opt.add_argument("--max-tokens", type=_positive_int, default=None,
                     help=(
                         "Request a higher output-token limit for model calls. "
                         "Values below the built-in limits have no effect."
                     ))
    opt.add_argument("--reasoning-effort", choices=REASONING_EFFORTS, default=None,
                     help="Model reasoning effort; omit to use the selected model's default")
    opt.add_argument("--provider-sort", choices=PROVIDER_SORTS, default=None,
                     help=(
                         "Rank the providers a request may use by this property; "
                         "omit to keep OpenRouter's default balancing"
                     ))
    opt.add_argument("--allow-data-retention", action="store_true",
                     help="Allow routing to providers that retain prompt data")
    opt.add_argument("--allow-data-collection", action="store_true",
                     help="Allow routing to providers that may collect or train on data")
    opt.add_argument("--force", action="store_true",
                     help=(
                         "Rebuild this command's results instead of reusing saved "
                         "results. If inputs or settings changed, choose a new --out "
                         "directory; --force does not make the old directory reusable."
                     ))
    opt.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    return p


def _solutions_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("solutions")
    g.add_argument("--solutions", "-s", default=None, metavar="PATH",
                   help=(
                       "Teacher answer key or solutions manual (supported document "
                       "format or JSON). Missing answers are generated and "
                       "independently checked by default."
                   ))
    g.add_argument("--strict-solutions", action="store_true",
                   help=(
                       "Stop if the provided answer key is incomplete instead of "
                       "generating missing answers"
                   ))
    g.add_argument("--verify-provided-solutions", action="store_true",
                   help=(
                       "Independently check supplied answers when building a "
                       "solutions manual; saved manuals are reused without rechecking"
                   ))


def _rubric_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("rubric")
    g.add_argument("--rubric", "-r", default=None, metavar="PATH",
                   help=(
                       "Teacher rubric (supported document format or JSON). Missing "
                       "problem entries are generated and labeled [auto-generated] "
                       "by default."
                   ))
    g.add_argument("--rubric-prompt", default=None, metavar="TEXT",
                   help="Instructor preferences steering rubric generation, e.g. "
                        "\"weight setup over arithmetic; no credit for unjustified answers\"")
    g.add_argument("--strict-rubric", action="store_true",
                   help=(
                       "Stop when any assignment problem lacks a rubric entry "
                       "instead of generating the missing entry"
                   ))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autograder",
        description="Agentic autograder for physics/math assignments (PDF, images, markdown, LaTeX).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    parent = _parent_parser()

    sub.add_parser(
        "inspect",
        parents=[parent],
        help="Read the assignment and save its problems, parts, and point values",
    )

    p_solve = sub.add_parser(
        "solve",
        parents=[parent],
        help="Create or check an answer key and save a solutions manual",
    )
    _solutions_args(p_solve)

    p_rubric = sub.add_parser(
        "rubric",
        parents=[parent],
        help=(
            "Create or check a grading rubric "
            "(also creates or checks the answer key)"
        ),
    )
    _solutions_args(p_rubric)
    _rubric_args(p_rubric)

    p_grade = sub.add_parser(
        "grade",
        parents=[parent],
        help=(
            "Grade every submission and write reports, a summary, "
            "and a review queue"
        ),
    )
    _solutions_args(p_grade)
    _rubric_args(p_grade)
    g = p_grade.add_argument_group("grading")
    g.add_argument("--submissions", "-S", required=True, nargs="+", metavar="PATH",
                   help="Submission file(s) or directory (subdirectory per student, or one file per student)")
    g.add_argument("--review-confidence", type=_unit_float, default=0.60, metavar="X",
                   help=(
                       "Grading results with model confidence below this value go "
                       "to the human review queue (default: 0.60)"
                   ))
    g.add_argument("--ocr-threshold", type=_unit_float, default=0.50, metavar="X",
                   help=(
                       "Transcriptions with model confidence below this value go "
                       "to the human review queue (default: 0.50)"
                   ))
    return parser


def _to_config(args: argparse.Namespace) -> RunConfig:
    cfg = RunConfig(
        model=args.model,
        api_key=args.api_key or os.environ.get("OPENROUTER_API_KEY"),
        max_workers=args.max_workers,
        reasoning_effort=args.reasoning_effort,
        provider_sort=args.provider_sort,
        zero_data_retention=not args.allow_data_retention,
        allow_data_collection=args.allow_data_collection,
        force=args.force,
        verbose=args.verbose,
    )
    if args.max_tokens:
        cfg.max_tokens = max(cfg.max_tokens, args.max_tokens)
        cfg.big_max_tokens = max(cfg.big_max_tokens, args.max_tokens)
    if getattr(args, "strict_solutions", False):
        cfg.strict_solutions = True
    if getattr(args, "verify_provided_solutions", False):
        cfg.verify_provided_solutions = True
    if getattr(args, "strict_rubric", False):
        cfg.strict_rubric = True
    if getattr(args, "review_confidence", None) is not None:
        cfg.review_confidence = args.review_confidence
    if getattr(args, "ocr_threshold", None) is not None:
        cfg.ocr_review_threshold = args.ocr_threshold
    return cfg


def _check_key(cfg: RunConfig) -> None:
    if not cfg.api_key:
        log.warning(
            "OPENROUTER_API_KEY is not set. Saved results can still be reused, but "
            "the command will stop if it needs to call the model. Set the environment "
            "variable or pass --api-key."
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openrouter").setLevel(logging.WARNING)

    try:
        # Built inside the handler so an invalid option combination reports the
        # same one-line error as every other startup failure, not a traceback.
        cfg = _to_config(args)
        _check_key(cfg)
        pipe = Pipeline(cfg, Path(args.assignment), Path(args.out))
        if args.command == "inspect":
            spec = pipe.run_inspect()
            print(f"\n{spec.title}: {len(spec.leaves())} gradable problem(s), "
                  f"total points: {spec.total_points if spec.total_points is not None else 'not printed'}")
            print(
                f"Assignment structure written to "
                f"{Path(args.out) / 'assignment_spec.json'}"
            )
        elif args.command == "solve":
            manual = pipe.run_solve(Path(args.solutions) if args.solutions else None)
            n_unverified = sum(
                1 for solution in manual.solutions.values() if not solution.verified
            )
            suffix = "entry" if len(manual.solutions) == 1 else "entries"
            print(
                f"\nSolutions manual: {len(manual.solutions)} {suffix}; "
                f"{n_unverified} marked unverified."
            )
            manual_path = Path(args.out) / "solutions_manual.md"
            print(f"Review {manual_path} before grading.")

            unavailable_checks = sum(
                issue.message.startswith("could not verify provided solution")
                for issue in pipe.issues
            )
            if unavailable_checks:
                answer = "answer" if unavailable_checks == 1 else "answers"
                pronoun = "it" if unavailable_checks == 1 else "them"
                print(
                    f"Independent checking was unavailable for "
                    f"{unavailable_checks} supplied {answer}."
                )
                print(
                    f"This failure does not mark the {answer} unverified or add "
                    "dependent grades to the review queue."
                )
                print(
                    f"Review {pronoun} manually, or resolve the evaluator failure "
                    "and retry with a new --out directory."
                )
                print(f"Details: {Path(args.out) / 'run_manifest.json'}")
        elif args.command == "rubric":
            rubric = pipe.run_rubric(Path(args.solutions) if args.solutions else None,
                                     Path(args.rubric) if args.rubric else None,
                                     args.rubric_prompt)
            print(f"\nRubric: {len(rubric.problems)} problem(s), total {rubric.total_points:g} pt.")
            print(f"Written to {Path(args.out) / 'rubric.md'}")
        elif args.command == "grade":
            grades = pipe.run_grade(args.submissions,
                                    Path(args.solutions) if args.solutions else None,
                                    Path(args.rubric) if args.rubric else None,
                                    args.rubric_prompt)
            print(f"\nGraded {len(grades)} student(s).")
            for g in sorted(grades, key=lambda s: s.student_id):
                n_rev = sum(1 for p in g.problems.values() if p.needs_review)
                rev = f"  [{n_rev} to review]" if n_rev else ""
                print(f"  {g.student_id}: {g.total_awarded:g} / {g.total_possible:g}{rev}")
            print(f"\nSummary: {Path(args.out) / 'summary.csv'}")
            print(f"Review queue: {Path(args.out) / 'review_queue.md'}")
        return 0
    except KeyboardInterrupt:
        log.error(
            "Interrupted. Run the same command again with the same inputs and --out "
            "directory to resume."
        )
        return 130
    except PartialGradeFailure as exc:
        incomplete = sum(not grade.score_complete for grade in exc.grades)
        print(
            f"\nGrading finished with incomplete results: {incomplete} incomplete "
            f"student record(s), {len(exc.failures)} student failure(s)."
        )
        for grade in sorted(exc.grades, key=lambda item: item.student_id):
            if grade.score_complete:
                score = f"{grade.total_awarded:g} / {grade.total_possible:g}"
            else:
                score = (
                    "final score unavailable "
                    f"({grade.processed_awarded:g} / {grade.processed_possible:g} processed)"
                )
            print(f"  {grade.student_id}: {score}")
        for failure in sorted(exc.failures, key=lambda item: item.student_id):
            print(f"  {failure.student_id}: failed ({failure.message})")
        print(f"\nSummary: {Path(args.out) / 'summary.csv'}")
        print(f"Review queue: {Path(args.out) / 'review_queue.md'}")
        return 2
    except Exception as exc:
        log.error("%s", exc)
        if args.verbose:  # not cfg.verbose: cfg may not exist yet
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
