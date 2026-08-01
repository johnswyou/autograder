"""Focused structural guards for maintained documentation."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import fields
from pathlib import Path
from urllib.parse import unquote

import pytest

from autograder import __version__
from autograder.cli import build_parser
from autograder.config import RunConfig
from autograder.models import Rubric
from autograder.solutions import parse_provided_solutions

ROOT = Path(__file__).resolve().parents[1]
MAINTAINED_MARKDOWN = (
    ROOT / "README.md",
    ROOT / "docs" / "README.md",
    ROOT / "docs" / "getting-started.md",
    ROOT / "docs" / "how-it-works.md",
    ROOT / "docs" / "usage.md",
    ROOT / "docs" / "reference.md",
    ROOT / "docs" / "architecture.md",
)
DOCUMENTATION_INDEX = ROOT / "docs" / "README.md"
NEW_GUIDES = (
    ROOT / "docs" / "getting-started.md",
    ROOT / "docs" / "how-it-works.md",
    ROOT / "docs" / "reference.md",
)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
MERMAID_DECLARATIONS = (
    "flowchart ",
    "graph ",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "erDiagram",
    "journey",
    "gantt",
    "pie",
    "mindmap",
    "timeline",
    "gitGraph",
    "quadrantChart",
    "xychart",
    "sankey-beta",
)


def _marked_block(text: str, name: str) -> str:
    match = re.search(
        rf"<!-- {re.escape(name)}:start -->\n(.*?)\n<!-- {re.escape(name)}:end -->",
        text,
        flags=re.DOTALL,
    )
    assert match is not None, f"reference is missing the {name!r} contract block"
    return match.group(1)


def _marked_table(text: str, name: str) -> list[dict[str, str]]:
    lines = [line for line in _marked_block(text, name).splitlines() if line.startswith("|")]
    assert len(lines) >= 2, f"{name!r} must contain a Markdown table"

    def cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    headings = cells(lines[0])
    assert all(re.fullmatch(r":?-+:?", cell) for cell in cells(lines[1]))
    rows = [dict(zip(headings, cells(line), strict=True)) for line in lines[2:]]
    assert rows, f"{name!r} must contain at least one data row"
    return rows


def _code_value(cell: str) -> str:
    match = re.fullmatch(r"`([^`]+)`", cell)
    assert match is not None, f"expected one code-formatted value, got {cell!r}"
    return match.group(1)


def _contract_default(value: object, *, required: bool = False) -> str:
    if required:
        return "required"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _marked_json(text: str, name: str) -> object:
    block = _marked_block(text, name)
    match = re.fullmatch(r"```json\n(.*)\n```", block, flags=re.DOTALL)
    assert match is not None, f"{name!r} must contain exactly one JSON code fence"
    return json.loads(match.group(1))


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


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _local_link_targets(text: str) -> list[str]:
    targets = []
    for raw_target in MARKDOWN_LINK.findall(text):
        target = unquote(raw_target.strip())
        if not target.startswith(("http://", "https://", "mailto:")):
            targets.append(target)
    return targets


def _resolve_local_link(path: Path, file_part: str) -> Path:
    target_path = Path(file_part)
    assert not target_path.is_absolute(), (
        f"{path.relative_to(ROOT)} must use a relative local link: {file_part}"
    )

    linked_path = (path.parent / target_path).resolve()
    assert linked_path.is_relative_to(ROOT), (
        f"{path.relative_to(ROOT)} links outside the repository: {file_part}"
    )
    return linked_path


def test_local_link_parser_includes_line_start_links_and_excludes_images() -> None:
    targets = _local_link_targets("[Guide](reference.md)\n![Diagram](diagram.png)")

    assert targets == ["reference.md"]


def test_local_link_paths_must_be_relative() -> None:
    with pytest.raises(AssertionError, match="relative"):
        _resolve_local_link(ROOT / "README.md", "/etc/passwd")


def test_local_link_paths_must_stay_in_repository() -> None:
    with pytest.raises(AssertionError, match="repository"):
        _resolve_local_link(DOCUMENTATION_INDEX, "../../outside.md")


def test_local_parent_link_can_resolve_within_repository() -> None:
    linked_path = _resolve_local_link(DOCUMENTATION_INDEX, "../README.md")

    assert linked_path == ROOT / "README.md"


def test_required_documentation_set_exists() -> None:
    missing = [
        path.relative_to(ROOT).as_posix()
        for path in MAINTAINED_MARKDOWN
        if not path.is_file()
    ]

    assert not missing, f"missing maintained documentation: {', '.join(missing)}"


def test_reference_cli_option_table_catches_parser_option_and_scope_drift() -> None:
    reference = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    rows = _marked_table(reference, "cli-options")
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    commands = set(subparsers.choices)
    actual = {
        (command, option)
        for command, command_parser in subparsers.choices.items()
        for action in command_parser._actions
        if not isinstance(action, argparse._HelpAction)
        for option in action.option_strings
    }

    documented: set[tuple[str, str]] = set()
    for row in rows:
        scopes = _code_value(row["Commands"])
        row_commands = commands if scopes == "all" else set(scopes.split(", "))
        assert row_commands <= commands, f"unknown documented command scope: {scopes}"
        options = re.findall(r"`(-{1,2}[^`]+)`", row["Options"])
        assert options, f"CLI row has no option aliases: {row}"
        documented.update(
            (command, option) for command in row_commands for option in options
        )

    assert documented == actual, (
        "CLI reference drift; missing parser options/scopes: "
        f"{sorted(actual - documented)}; stale options/scopes: "
        f"{sorted(documented - actual)}"
    )


def test_reference_cli_option_table_catches_parser_default_drift() -> None:
    reference = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    rows = _marked_table(reference, "cli-options")
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    commands = set(subparsers.choices)
    actual = {
        (command, action.dest): _contract_default(
            action.default,
            required=action.required,
        )
        for command, command_parser in subparsers.choices.items()
        for action in command_parser._actions
        if not isinstance(action, argparse._HelpAction)
    }

    documented: dict[tuple[str, str], str] = {}
    for row in rows:
        scopes = _code_value(row["Commands"])
        row_commands = commands if scopes == "all" else set(scopes.split(", "))
        destination = _code_value(row["Destination"])
        default = _code_value(row["Parser default"])
        for command in row_commands:
            documented[(command, destination)] = default

    assert documented == actual, (
        "CLI parser-default drift; update only the structured default cells: "
        f"expected {actual}, documented {documented}"
    )


def test_reference_runconfig_tables_catch_field_and_default_drift() -> None:
    reference = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    rows = [
        *_marked_table(reference, "runconfig-public"),
        *_marked_table(reference, "runconfig-advanced"),
    ]
    documented = {
        _code_value(row["Field"]): _code_value(row["Default"]) for row in rows
    }
    actual = {
        field.name: _contract_default(field.default) for field in fields(RunConfig)
    }

    assert documented == actual, (
        "RunConfig reference drift; a field is missing, stale, or has a changed default: "
        f"expected {actual}, documented {documented}"
    )


def test_reference_solution_json_example_matches_the_accepted_parser_shape(
    tmp_path: Path,
) -> None:
    reference = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    example = _marked_json(reference, "solution-json-example")
    key_path = tmp_path / "solutions.json"
    key_path.write_text(json.dumps(example), encoding="utf-8")

    solutions, issues = parse_provided_solutions(
        None,
        RunConfig(),
        None,
        key_path,
        None,
        None,
    )

    assert issues == []
    assert set(solutions) == {"1"}
    assert solutions["1"].final_answer


def test_reference_rubric_json_example_matches_the_pydantic_input_shape() -> None:
    reference = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    example = _marked_json(reference, "rubric-json-example")

    rubric = Rubric.model_validate(example)

    assert [problem.problem_id for problem in rubric.problems] == ["1"]
    assert rubric.problems[0].criteria


def test_documentation_index_routes_each_reader_path() -> None:
    index = DOCUMENTATION_INDEX.read_text(encoding="utf-8")
    targets = set(_local_link_targets(index))

    assert {
        "../README.md",
        "getting-started.md",
        "how-it-works.md",
        "usage.md",
        "reference.md",
        "architecture.md",
    } <= targets


def test_guides_link_back_to_the_documentation_index() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    root_targets = set(_local_link_targets(readme))
    assert "docs/README.md" in root_targets

    for path in NEW_GUIDES:
        targets = set(_local_link_targets(path.read_text(encoding="utf-8")))
        assert "README.md" in targets, (
            f"{path.relative_to(ROOT)} must link back to docs/README.md"
        )


def test_maintained_markdown_links_anchors_and_fences_are_valid() -> None:
    for path in MAINTAINED_MARKDOWN:
        text = path.read_text(encoding="utf-8")
        assert sum(
            line.lstrip().startswith("```") for line in text.splitlines()
        ) % 2 == 0, f"{path.relative_to(ROOT)} has an unclosed code fence"

        anchors = _heading_anchors(text)
        for target in _local_link_targets(text):
            if target.startswith("#"):
                assert target[1:] in anchors, (
                    f"{path.relative_to(ROOT)} has missing anchor {target}"
                )
                continue

            file_part, separator, anchor = target.partition("#")
            linked_path = _resolve_local_link(path, file_part)
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


def test_mermaid_blocks_have_recognized_diagram_types() -> None:
    for path in MAINTAINED_MARKDOWN:
        text = path.read_text(encoding="utf-8")
        blocks = re.findall(r"```mermaid\s*\n(.*?)```", text, flags=re.DOTALL)
        for block in blocks:
            first_line = next(
                line.strip() for line in block.splitlines() if line.strip()
            )
            assert first_line.startswith(MERMAID_DECLARATIONS), (
                f"{path.relative_to(ROOT)} has an unrecognized Mermaid block: "
                f"{first_line}"
            )


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
            "https://github.com/johnswyou/autograder/blob/main/docs/README.md"
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


def test_rubric_docstrings_do_not_call_generated_json_immutable() -> None:
    rubric_source = (ROOT / "autograder" / "rubric.py").read_text(
        encoding="utf-8"
    ).lower()

    assert "immutable implementation state" not in rubric_source
    assert "cached implementation state" not in rubric_source
    assert rubric_source.count("pipeline-owned resume data") >= 2


def test_solution_docstring_describes_generated_gap_checks_precisely() -> None:
    solution_source = (ROOT / "autograder" / "solutions.py").read_text(
        encoding="utf-8"
    )

    normalized = _normalized(solution_source)
    assert (
        "gaps go through the same solver/evaluator process, with the incomplete-"
        "key warning recorded"
    ) in normalized
    assert "gaps are generated (and flagged)" not in solution_source


def test_orchestrator_docstring_describes_conditional_reuse() -> None:
    orchestrator_source = (ROOT / "autograder" / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    normalized = _normalized(orchestrator_source)

    assert (
        "Eligible saved stage results are reused when `--force` is absent."
        in orchestrator_source
    )
    assert (
        "Invalid results are rebuilt, and failed per-problem results are "
        "retried while successful siblings are retained."
    ) in normalized
    assert "is skipped on\nre-run if the artifact already exists" not in (
        orchestrator_source
    )
