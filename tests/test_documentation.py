"""Focused guards against user-facing documentation drift."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from autograder import __version__
from autograder.config import DEFAULT_MODEL, SUPPORTED_EXTS, RunConfig

ROOT = Path(__file__).resolve().parents[1]
MAINTAINED_MARKDOWN = (
    ROOT / "README.md",
    ROOT / "docs" / "usage.md",
    ROOT / "docs" / "architecture.md",
)
TEACHER_FACING_MARKDOWN = (
    ROOT / "README.md",
    ROOT / "docs" / "usage.md",
)
OPAQUE_TEACHER_TERMS = (
    "implementation state",
    "teacher-editing interface",
    "teacher-edit interface",
    "normal-bound",
    "force-bound",
    "cache-relevant",
    "run binding",
    "normal-mode",
    "pass 1",
    "fail closed",
    "canonical",
    "artifact",
    "leaf",
)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _heading_anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.startswith("#"):
            continue
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if match is None:
            continue
        heading = re.sub(r"<[^>]+>", "", match.group(1)).lower()
        heading = re.sub(r"[^\w\- ]", "", heading)
        slug = heading.replace(" ", "-")
        duplicate = occurrences.get(slug, 0)
        occurrences[slug] = duplicate + 1
        anchors.add(slug if duplicate == 0 else f"{slug}-{duplicate}")
    return anchors


def test_documented_default_model_matches_config() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    references = {
        "README flag summary": re.search(r"`--model` \(default `([^`]+)`\)", readme),
        "usage flag table": re.search(r"\| `--model` \| \| `([^`]+)` \|", usage),
        "usage model guidance": re.search(r"The default is `([^`]+)`", usage),
    }

    for location, match in references.items():
        assert match is not None, f"{location} is missing"
        assert match.group(1) == DEFAULT_MODEL, (
            f"{location} says {match.group(1)!r}; config defaults to {DEFAULT_MODEL!r}"
        )


def test_documented_cli_defaults_match_config() -> None:
    usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    expected_rows = (
        f"| `--max-workers` | | `{RunConfig.max_workers}` |",
        f"| `--max-tokens` | | `{RunConfig.max_tokens}` |",
        f"| `--thinking` | | `{RunConfig.thinking}` |",
        (
            "| `--review-confidence` | | "
            f"`{RunConfig.review_confidence:.2f}` |"
        ),
        (
            "| `--ocr-threshold` | | "
            f"`{RunConfig.ocr_review_threshold:.2f}` |"
        ),
    )

    for row_start in expected_rows:
        assert row_start in usage


def test_documented_assignment_extensions_match_config() -> None:
    usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    assignment_row = next(
        line for line in usage.splitlines()
        if line.startswith("| `--assignment` |")
    )
    documented = set(re.findall(r"`(\.[a-z]+)`", assignment_row))

    assert documented == SUPPORTED_EXTS


def test_license_and_project_urls_are_published() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert 'requires = ["setuptools>=77"]' in pyproject
    assert 'license = "MIT"' in pyproject
    assert 'license-files = ["LICENSE"]' in pyproject
    assert 'license = { text = "MIT" }' not in pyproject
    expected_urls = {
        "Homepage": "https://github.com/johnswyou/autograder",
        "Repository": "https://github.com/johnswyou/autograder",
        "Documentation": (
            "https://github.com/johnswyou/autograder/blob/main/docs/usage.md"
        ),
        "Issues": "https://github.com/johnswyou/autograder/issues",
    }
    for label, url in expected_urls.items():
        assert f'{label} = "{url}"' in pyproject

    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 John You" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text


def test_distribution_and_runtime_versions_match() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version = "([^"]+)"$', pyproject)

    assert match is not None, "[project].version is missing"
    assert match.group(1) == __version__


def test_documented_point_allocation_policy_matches_implementation() -> None:
    readme = _normalized(
        (ROOT / "README.md").read_text(encoding="utf-8")
    )
    usage = _normalized(
        (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    )
    architecture = _normalized(
        (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    )

    assert (
        "Every rubric problem weight must agree with the assignment's printed "
        "problem and total values; a conflict stops the command."
    ) in readme
    assert (
        "Each supplied problem weight must agree with every applicable point "
        "value printed on the assignment—including the value for that problem "
        "or subproblem, a parent-problem total, and the assignment total. A "
        "contradiction stops the command; the program does not change the "
        "supplied problem weight."
    ) in usage
    assert (
        "A supplied problem weight that conflicts with an explicit gradable-"
        "leaf value, parent total, or assignment total raises "
        "`PointAllocationError` instead of being normalized."
    ) in architecture
    assert "evenly divides a printed total" not in usage
    assert "complete teacher rubric" in usage
    assert (
        "Only an assignment with no printed point values and no printed total "
        "defaults to **1 point for each lowest-level problem or subproblem**."
    ) in usage


def test_documented_solution_rounds_match_config() -> None:
    usage = _normalized(
        (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    )
    match = re.search(
        r"By default, that allows one initial solver/evaluator attempt plus up "
        r"to (\d+) regeneration attempts \((\d+) total attempts\)\.",
        usage,
    )

    assert match is not None
    assert int(match.group(1)) == RunConfig.solution_max_rounds
    assert int(match.group(2)) == RunConfig.solution_max_rounds + 1
    assert "solution_max_rounds" not in usage


def test_documented_tool_image_limit_matches_config() -> None:
    usage = _normalized(
        (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    )
    match = re.search(
        r"one agent retains up to (\d+) tool-result images", usage
    )

    assert match is not None
    assert int(match.group(1)) == RunConfig.max_tool_images
    assert "max_tool_images" not in usage


def test_usage_guide_is_organized_around_operator_tasks() -> None:
    usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    required_headings = (
        "## Prepare your inputs",
        "## Choose a command",
        "## Run and review a grading job",
        "## Provide or revise an answer key",
        "## Provide or revise a rubric",
        "## Resume or change a grading job",
        "## Troubleshooting",
    )

    positions = [usage.index(heading) for heading in required_headings]
    assert positions == sorted(positions)


def test_usage_privacy_warning_is_plain_and_actionable() -> None:
    usage = _normalized(
        (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    )

    assert (
        "**Student data is sent to Anthropic, and model calls cost money.**"
        in usage
    )
    assert "Student data and API charges leave your computer" not in usage
    assert "Confirm that your institution permits this use" in usage
    assert "before grading real student work." in usage


def test_usage_guide_explains_generated_files_and_changed_inputs() -> None:
    usage = _normalized(
        (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    )

    assert (
        "Treat files inside `--out` as read-only."
    ) in usage
    assert (
        "When a job resumes, the autograder may reuse saved work instead of "
        "repeating it."
    ) in usage
    assert (
        "Reusable records are the assignment structure (`assignment_spec.json`), "
        "solutions (`solutions_manual.json`), rubric (`rubric.json`), and each "
        "student's mapping (`mapping.json`), transcripts (`transcripts.json`), "
        "and grades (`grades.json`)."
    ) in usage
    assert (
        "If a saved record is invalid or contains failed work eligible for "
        "retry, the autograder may rewrite that file."
    ) in usage
    assert (
        "A manual edit can therefore affect the resumed job or be lost."
    ) in usage
    assert (
        "To change an answer key or rubric, edit the source file outside the "
        "output directory, pass it with `--solutions` or `--rubric`, and use a "
        "new `--out` directory."
    ) in usage
    assert (
        "`--force` rebuilds stages; it does not override a mismatch detected for "
        "recorded inputs or settings."
    ) in usage
    assert "editing them does not supply new input" not in usage


def test_teacher_facing_docs_avoid_opaque_implementation_terms() -> None:
    for path in TEACHER_FACING_MARKDOWN:
        text = path.read_text(encoding="utf-8").lower()
        for phrase in OPAQUE_TEACHER_TERMS:
            assert phrase not in text, (
                f"{path.relative_to(ROOT)} still uses {phrase!r}"
            )


def test_architecture_uses_headings_and_defines_core_terms() -> None:
    architecture = (
        ROOT / "docs" / "architecture.md"
    ).read_text(encoding="utf-8")

    assert not re.search(r"(?m)^\*\*[^*]+\*\*$", architecture)
    for heading in (
        "## Architectural overview",
        "## System layers",
        "## Safe persistence and reuse",
        "## Pipeline stages",
        "## Agent runtime",
        "## Data model",
        "## Programmatic integration",
        "## Testing the architecture",
        "## Design decisions",
    ):
        assert heading in architecture
    assert (
        "An **artifact** is a generated, structured file that one pipeline "
        "stage writes for another stage to read."
    ) in _normalized(architecture)
    assert (
        "A **gradable leaf** is the lowest-level problem or subproblem that "
        "receives its own rubric entry and score."
    ) in _normalized(architecture)
    assert (
        "The `solution_max_rounds` configuration field allows up to "
        f"{RunConfig.solution_max_rounds} regeneration attempts after the "
        "initial solver/evaluator attempt "
        f"({RunConfig.solution_max_rounds + 1} total attempts by default)."
    ) in _normalized(architecture)
    assert (
        "The `max_tool_images` configuration field controls how many "
        f"tool-result images one agent retains ({RunConfig.max_tool_images} "
        "by default)."
    ) in _normalized(architecture)


def test_architecture_documents_current_run_binding_schema() -> None:
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    assert "The schema-version-2 file records:" in architecture


def test_mermaid_blocks_have_recognized_diagram_types() -> None:
    allowed = ("flowchart ", "sequenceDiagram", "classDiagram")

    for path in MAINTAINED_MARKDOWN:
        text = path.read_text(encoding="utf-8")
        blocks = re.findall(r"```mermaid\s*\n(.*?)```", text, flags=re.DOTALL)
        for block in blocks:
            first_line = next(
                line.strip() for line in block.splitlines() if line.strip()
            )
            assert first_line.startswith(allowed), (
                f"{path.relative_to(ROOT)} has an unrecognized Mermaid block: "
                f"{first_line}"
            )


def test_answer_key_docs_distinguish_validation_from_verification() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    architecture = (
        ROOT / "docs" / "architecture.md"
    ).read_text(encoding="utf-8")
    normalized_usage = " ".join(usage.split())
    normalized_architecture = " ".join(architecture.split())

    assert "coverage and non-empty content" in readme
    assert "independent correctness verification" in readme
    assert "`--verify-provided-solutions`" in readme
    assert (
        "content/mapping validation, not mathematical verification"
        in normalized_usage
    )
    assert (
        "By default, a missing or empty entry is generated and sent through "
        "the same solver/evaluator check used when no key is supplied. Only "
        "an answer that remains unverified sends dependent grades to review."
    ) in normalized_usage
    assert (
        "Missing entries are generated with the same solver/evaluator process "
        "unless `--strict-solutions` requests an error. A generated answer "
        "that still fails evaluation remains unverified and sends dependent "
        "grades to review."
    ) in normalized_architecture
    assert (
        "For a generated answer, `verified` means it passed the evaluator and "
        "every prerequisite solution is verified. For a supplied answer, "
        "`verified` means the entry was matched to an assignment problem and "
        "every prerequisite solution is verified; it does not by itself mean "
        "the mathematics was independently checked. "
        "`--verify-provided-solutions` requests that separate check, and any "
        "failed check is recorded."
    ) in normalized_usage
    assert (
        "`Solution.verified` is provenance-sensitive: generated answers require "
        "evaluator success, while supplied answers normally require successful "
        "problem matching. In both cases, a dependent solution is verified only "
        "when every prerequisite solution is verified. With "
        "`--verify-provided-solutions`, a negative evaluator verdict clears the "
        "status; an evaluator infrastructure failure records an issue and "
        "preserves the prior matching status."
    ) in normalized_architecture
    assert (
        "If the evaluator cannot run, the supplied entry keeps its problem-"
        "matching status even though the requested correctness check did not "
        "finish."
    ) in normalized_usage
    assert (
        "This failure alone does not mark the answer unverified or send "
        "dependent grades to the review queue. The saved manual does not keep "
        "a separate “check unavailable” status, and reusing it does not retry "
        "the check."
    ) in normalized_usage
    assert (
        "Review the affected answer manually, or resolve the evaluator failure "
        "and repeat the job with a new `--out` directory."
    ) in normalized_usage
    assert "generated and marked unverified" not in normalized_usage
    assert "generated and marked unverified" not in normalized_architecture
    assert "whether it was verified" not in normalized_usage


def test_rubric_docs_describe_strict_mode_scope() -> None:
    usage = _normalized(
        (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    )

    assert (
        "`--strict-rubric` stops when any assignment problem lacks a rubric "
        "entry instead of generating the missing entry."
    ) in usage
    assert (
        "| `--strict-rubric` | | off | Stops when any assignment problem lacks "
        "a rubric entry instead of generating the missing entry. |"
    ) in usage


def test_troubleshooting_matches_missing_key_and_student_failure_behavior() -> None:
    usage = _normalized(
        (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    )

    assert "A student is absent from the summary" not in usage
    assert "A student row has `run_status` set to `failed`" in usage
    assert "Read the row's `failure` column" in usage
    assert "The CLI warns that `ANTHROPIC_API_KEY` is not set" in usage
    assert (
        "Saved results can still be reused, but the command stops if it needs "
        "a model call."
    ) in usage


def test_security_docs_describe_transcript_escaping_precisely() -> None:
    usage = _normalized(
        (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    )

    assert (
        "Student transcripts are HTML-escaped before being included in "
        "Markdown reports so submission text cannot close the transcript "
        "block or inject raw HTML."
    ) in usage
    assert "cannot inject markup into those reports" not in usage


def test_programmatic_example_reads_the_api_key_explicitly() -> None:
    architecture = (
        ROOT / "docs" / "architecture.md"
    ).read_text(encoding="utf-8")

    assert "import os" in architecture
    assert 'api_key=os.environ.get("ANTHROPIC_API_KEY")' in architecture


def test_programmatic_example_uses_one_pipeline_entry_point() -> None:
    architecture = (
        ROOT / "docs" / "architecture.md"
    ).read_text(encoding="utf-8")
    section = architecture.split("## Programmatic integration", 1)[1]
    example = re.search(r"```python\n(.*?)```", section, flags=re.DOTALL)

    assert example is not None
    assert example.group(1).count("pipeline.run_") == 1
    assert "grades = pipeline.run_grade(" in example.group(1)
    assert (
        "Call exactly one public `run_*` method on a `Pipeline` instance."
        in section
    )
    assert (
        "Every `run_*` method releases the assignment document on the way out, "
        "including when it raises, so callers do not have to close it themselves."
    ) in _normalized(section)
    assert "pipeline.assignment.close()" not in section, (
        "the example must not teach a cleanup step the pipeline now handles"
    )
    assert "except PartialGradeFailure as exc:" in example.group(1)
    assert "grades = exc.grades" in example.group(1)
    assert "failures = exc.failures" in example.group(1)


def test_docs_state_submission_roster_binding_limit() -> None:
    usage = _normalized(
        (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    )
    architecture = _normalized(
        (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    )

    assert (
        "Adding or removing a student in the same output directory may not be "
        "rejected: class-level files can be rewritten for the new roster, and "
        "an old student's directory can remain."
    ) in usage
    assert (
        "`RunState` does not store or compare one digest for the complete "
        "submission roster."
    ) in architecture


def test_architecture_allows_commands_to_extend_a_compatible_run() -> None:
    architecture = _normalized(
        (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    )

    assert (
        "The command name is not part of the binding, so a later command can "
        "extend a compatible run—for example, `grade` can reuse assignment "
        "work written by `inspect`."
    ) in architecture
    assert "when the command, inputs, and settings still match" not in architecture


def test_readme_review_table_does_not_limit_unverified_status_to_generated_keys() -> None:
    readme = _normalized((ROOT / "README.md").read_text(encoding="utf-8"))

    assert (
        "The program adds items to `review_queue.md` for situations including:"
        in readme
    )
    assert (
        "| The solution used for a problem is marked unverified | Review every "
        "grade that depends on that solution."
    ) in readme
    assert "A generated solution remains unverified" not in readme


def test_docs_state_the_unverified_solution_review_exemption() -> None:
    """`grade_problem` awards `blank`/`not_found` zeros without consulting the
    solution, so those problems are not queued when the solution is unverified.
    Stating the rule without the exemption overpromises."""
    readme = _normalized((ROOT / "README.md").read_text(encoding="utf-8"))
    usage = _normalized((ROOT / "docs" / "usage.md").read_text(encoding="utf-8"))

    assert (
        "Problems scored zero for `blank` or `not_found` are exempt: no solution "
        "is consulted to award zero."
    ) in readme
    assert (
        "the solution used for grading is unverified — except on the `blank` and "
        "`not_found` paths, which award zero without consulting the solution and "
        "so do not need a verified one;"
    ) in usage


def test_architecture_states_generated_file_integrity_boundary() -> None:
    architecture = _normalized(
        (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    )

    assert (
        "Before considering saved work, `RunState` verifies the assignment and "
        "settings, compares the digest of every requested input whose name was "
        "recorded earlier, and records digests for new input names."
    ) in architecture
    assert (
        "`run_binding.json` verifies the assignment, settings, and values saved "
        "under previously recorded input names; it does not verify the complete "
        "submission roster or the contents of generated files."
    ) in architecture
    assert (
        "A valid manual JSON edit can therefore affect a later command, while "
        "an invalid or inconsistent edit may be normalized, rejected, or "
        "overwritten."
    ) in architecture
    assert (
        "Generated files are outputs, not a hidden input channel."
        not in architecture
    )
    assert "proven to represent the same grading setup" not in architecture


def test_rubric_docstrings_do_not_call_generated_json_immutable() -> None:
    rubric_source = (
        ROOT / "autograder" / "rubric.py"
    ).read_text(encoding="utf-8").lower()

    assert "immutable implementation state" not in rubric_source
    assert "cached implementation state" not in rubric_source
    assert rubric_source.count("pipeline-owned resume data") >= 2


def test_solution_docstring_describes_generated_gap_checks_precisely() -> None:
    solution_source = (
        ROOT / "autograder" / "solutions.py"
    ).read_text(encoding="utf-8")

    assert (
        "gaps go through the same solver/evaluator process, with the incomplete-"
        "key warning recorded"
    ) in _normalized(solution_source)
    assert "gaps are generated (and flagged)" not in solution_source


def test_orchestrator_docstring_describes_conditional_reuse() -> None:
    orchestrator_source = (
        ROOT / "autograder" / "orchestrator.py"
    ).read_text(encoding="utf-8")

    assert (
        "Eligible saved stage results are reused when `--force` is absent."
        in orchestrator_source
    )
    assert (
        "Invalid results are rebuilt, and failed per-problem results are "
        "retried while successful siblings are retained."
    ) in _normalized(orchestrator_source)
    assert "is skipped on\nre-run if the artifact already exists" not in orchestrator_source


def test_manifest_docs_describe_version_config_and_usage_scope() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "run_manifest.json" in readme

    required = (
        "tool version",
        "selected run configuration",
        "current command invocation",
    )
    for path in (
        ROOT / "docs" / "usage.md",
        ROOT / "docs" / "architecture.md",
    ):
        text = path.read_text(encoding="utf-8").lower()
        for phrase in required:
            assert phrase in text, (
                f"{path.relative_to(ROOT)} is missing manifest contract: {phrase}"
            )


def test_sample_is_described_as_synthetic_and_typeset() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")

    for document in (readme, usage):
        assert "synthetic" in document.lower()
        assert "typeset" in document.lower()
    assert "does not measure handwriting or OCR accuracy" in readme


def test_readme_explains_how_agents_read_small_handwriting() -> None:
    readme = _normalized(
        (ROOT / "README.md").read_text(encoding="utf-8")
    )

    assert "When handwriting is too small to read in a full-page view" in readme
    assert "cropped, higher-resolution view" in readme
    assert (
        "Zoom can enlarge detail that is present in the source, but it cannot "
        "restore detail missing from a blurry or low-resolution scan."
    ) in readme


def test_readme_explains_output_editing() -> None:
    readme = _normalized(
        (ROOT / "README.md").read_text(encoding="utf-8")
    )

    assert (
        "Treat files inside `--out` as read-only."
    ) in readme
    assert (
        "When a job resumes, the autograder may reuse saved work instead of "
        "repeating it."
    ) in readme
    assert (
        "Reusable records are the assignment structure (`assignment_spec.json`), "
        "solutions (`solutions_manual.json`), rubric (`rubric.json`), and each "
        "student's mapping (`mapping.json`), transcripts (`transcripts.json`), "
        "and grades (`grades.json`)."
    ) in readme
    assert (
        "If a saved record is invalid or contains failed work eligible for "
        "retry, the autograder may rewrite that file."
    ) in readme
    assert (
        "A manual edit can therefore affect the resumed job or be lost."
    ) in readme
    assert "another change" not in readme
    assert "does not change the answer key or rubric" not in readme
    assert (
        "The Markdown, CSV, and JSON files need no database or custom viewer."
        not in readme
    )


def test_readme_distinguishes_problem_and_student_failures() -> None:
    readme = _normalized(
        (ROOT / "README.md").read_text(encoding="utf-8")
    )

    assert (
        "One problem cannot be mapped, transcribed, or graded"
    ) in readme
    assert (
        "No score is assigned for that problem; the report shows a processed "
        "subtotal but no final total."
    ) in readme
    assert "An entire student fails before a report is completed" in readme
    assert (
        "`summary.csv` contains a `failed` row with blank scores, and the "
        "review queue records the failure."
    ) in readme


def test_readme_file_links_are_portable_package_metadata() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    expected = (
        "https://github.com/johnswyou/autograder/blob/main/docs/usage.md",
        "https://github.com/johnswyou/autograder/blob/main/docs/architecture.md",
        "https://github.com/johnswyou/autograder/blob/main/pyproject.toml",
        "https://github.com/johnswyou/autograder/blob/main/LICENSE",
    )

    for target in expected:
        assert f"]({target})" in readme


def test_maintained_markdown_links_anchors_and_fences_are_valid() -> None:
    for path in MAINTAINED_MARKDOWN:
        text = path.read_text(encoding="utf-8")
        assert sum(
            line.lstrip().startswith("```") for line in text.splitlines()
        ) % 2 == 0, f"{path.relative_to(ROOT)} has an unclosed code fence"

        anchors = _heading_anchors(text)
        for raw_target in MARKDOWN_LINK.findall(text):
            target = unquote(raw_target.strip())
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.startswith("#"):
                assert target[1:] in anchors, (
                    f"{path.relative_to(ROOT)} has missing anchor {target}"
                )
                continue

            file_part, separator, anchor = target.partition("#")
            linked_path = (path.parent / file_part).resolve()
            assert linked_path.is_file(), (
                f"{path.relative_to(ROOT)} links to missing {file_part}"
            )
            if separator and linked_path.suffix.lower() == ".md":
                linked_anchors = _heading_anchors(
                    linked_path.read_text(encoding="utf-8")
                )
                assert anchor in linked_anchors, (
                    f"{path.relative_to(ROOT)} links to missing "
                    f"{file_part}#{anchor}"
                )
