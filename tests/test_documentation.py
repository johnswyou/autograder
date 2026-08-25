"""Focused structural guards for maintained documentation."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import pytest

import autograder.cli as autograder_cli
from autograder import __version__
from autograder.cli import build_parser
from autograder.config import IMAGE_EXTS, SUPPORTED_EXTS, TEXT_EXTS, RunConfig
from autograder.models import AssignmentSpec, Problem, Rubric
from autograder.report import write_manifest
from autograder.run_state import RunBinding
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


def _contract_action_kind(action: argparse.Action) -> str:
    if isinstance(action, argparse._StoreTrueAction):
        return "store_true"
    if isinstance(action, argparse._StoreAction):
        return "store"
    if isinstance(action, argparse._AppendAction):
        return "append"
    return type(action).__name__


def _contract_nargs(action: argparse.Action) -> str:
    return "1" if action.nargs is None else str(action.nargs)


def _contract_choices(action: argparse.Action) -> str:
    if action.choices is None:
        return "any"
    return ", ".join(str(choice) for choice in action.choices)


def _marked_json(text: str, name: str) -> object:
    block = _marked_block(text, name)
    match = re.fullmatch(r"```json\n(.*)\n```", block, flags=re.DOTALL)
    assert match is not None, f"{name!r} must contain exactly one JSON code fence"
    return json.loads(match.group(1))


def _reference_text() -> str:
    return (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")


def _subparser_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )


def _commands_for_cli_row(
    row: dict[str, str],
    subcommands: set[str],
) -> set[str]:
    scopes = _code_value(row["Commands"])
    row_scopes = subcommands if scopes == "all" else set(scopes.split(", "))
    known_scopes = {"root", *subcommands}
    assert row_scopes <= known_scopes, f"unknown documented command scope: {scopes}"
    return row_scopes


def _parser_cli_action_contracts(
    parser: argparse.ArgumentParser,
) -> list[tuple[str, tuple[str, ...], str, str, str, str, str]]:
    subparsers = _subparser_action(parser)
    scoped_parsers = {"root": parser, **subparsers.choices}
    contracts = []
    for scope, scoped_parser in scoped_parsers.items():
        for action in scoped_parser._actions:
            # argparse adds one of these to the root and every subparser. It is
            # framework help, not a public option maintained by this project.
            if isinstance(action, argparse._HelpAction):
                continue
            if not action.option_strings:
                continue
            contracts.append(
                (
                    scope,
                    tuple(action.option_strings),
                    action.dest,
                    _contract_nargs(action),
                    _contract_action_kind(action),
                    _contract_choices(action),
                    _contract_default(action.default, required=action.required),
                )
            )
    return contracts


def _documented_cli_action_contracts(
    rows: list[dict[str, str]],
    subcommands: set[str],
) -> list[tuple[str, tuple[str, ...], str, str, str, str, str]]:
    contracts = []
    for row in rows:
        options = tuple(re.findall(r"`(-{1,2}[^`]+)`", row["Options"]))
        assert options, f"CLI row has no option aliases: {row}"
        destination = _code_value(row["Destination"])
        nargs = _code_value(row["Nargs"])
        action_kind = _code_value(row["Action"])
        choices = _code_value(row["Choices"])
        default = _code_value(row["Parser default"])
        contracts.extend(
            (scope, options, destination, nargs, action_kind, choices, default)
            for scope in _commands_for_cli_row(row, subcommands)
        )
    return contracts


def _assert_cli_subcommand_contract(
    rows: list[dict[str, str]],
    parser: argparse.ArgumentParser,
) -> None:
    documented = [_code_value(row["Command"]) for row in rows]
    duplicates = sorted(
        {command for command in documented if documented.count(command) > 1}
    )
    assert not duplicates, f"duplicate CLI subcommand row(s): {duplicates}"

    actual = set(_subparser_action(parser).choices)
    assert set(documented) == actual, (
        "CLI subcommand contract drift; missing parser commands: "
        f"{sorted(actual - set(documented))}; stale documented commands: "
        f"{sorted(set(documented) - actual)}"
    )


def _assert_cli_option_contract(
    rows: list[dict[str, str]],
    parser: argparse.ArgumentParser,
) -> None:
    _assert_cli_subcommand_contract(
        _marked_table(_reference_text(), "cli-subcommands"), parser
    )
    subcommands = set(_subparser_action(parser).choices)
    documented = _documented_cli_action_contracts(rows, subcommands)
    duplicates = sorted(
        {contract for contract in documented if documented.count(contract) > 1}
    )
    assert not duplicates, f"duplicate canonical CLI action row(s): {duplicates}"

    actual = _parser_cli_action_contracts(parser)
    assert set(documented) == set(actual), (
        "CLI action contract drift; missing parser actions: "
        f"{sorted(set(actual) - set(documented))}; stale documented actions: "
        f"{sorted(set(documented) - set(actual))}"
    )


def _runconfig_reference_rows() -> list[dict[str, str]]:
    reference = _reference_text()
    return [
        *_marked_table(reference, "runconfig-public"),
        *_marked_table(reference, "runconfig-advanced"),
    ]


def _assert_runconfig_field_contract(rows: list[dict[str, str]]) -> None:
    names = [_code_value(row["Field"]) for row in rows]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"duplicate RunConfig field row(s): {duplicates}"

    documented = {
        name: _code_value(row["Default"]) for name, row in zip(names, rows, strict=True)
    }
    actual = {
        field.name: _contract_default(field.default) for field in fields(RunConfig)
    }
    assert documented == actual, (
        "RunConfig reference drift; a field is missing, stale, or has a changed default: "
        f"expected {actual}, documented {documented}"
    )


def _document_format_suffixes(
    rows: list[dict[str, str]], source: str
) -> set[str]:
    return {
        suffix
        for row in rows
        if source in re.findall(r"`([^`]+)`", row["Source constants"])
        for suffix in re.findall(r"`(\.[a-z]+)`", row["Suffix"])
    }


def _assert_document_format_contract(rows: list[dict[str, str]]) -> None:
    expected = {
        "SUPPORTED_EXTS": SUPPORTED_EXTS,
        "IMAGE_EXTS": IMAGE_EXTS,
        "TEXT_EXTS": TEXT_EXTS,
    }
    documented_sources = {
        source
        for row in rows
        for source in re.findall(r"`([^`]+)`", row["Source constants"])
    }
    documented = {
        source: _document_format_suffixes(rows, source)
        for source in documented_sources
    }

    assert documented == expected, (
        "document-format constant drift; expected "
        f"{expected}, documented {documented}"
    )


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


@pytest.mark.parametrize(
    "output_root",
    ["runs/sample-demo", "runs/kinematics-quiz"],
    ids=["sample", "real-run"],
)
def test_documented_output_roots_are_ignored(output_root: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", output_root],
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 0, (
        f"documented output root {output_root!r} is not protected by .gitignore"
    )


def test_cli_contract_detects_aliases_rebound_to_the_wrong_destination() -> None:
    rows = [dict(row) for row in _marked_table(_reference_text(), "cli-options")]
    assignment = next(row for row in rows if row["Destination"] == "`assignment`")
    output = next(row for row in rows if row["Destination"] == "`out`")
    assignment["Options"], output["Options"] = output["Options"], assignment["Options"]

    with pytest.raises(AssertionError, match="CLI action contract drift"):
        _assert_cli_option_contract(rows, build_parser())


def test_cli_contract_rejects_a_duplicate_canonical_action_row() -> None:
    rows = [dict(row) for row in _marked_table(_reference_text(), "cli-options")]
    rows.append(dict(rows[0]))

    with pytest.raises(AssertionError, match="duplicate canonical CLI action"):
        _assert_cli_option_contract(rows, build_parser())


def test_cli_contract_catches_an_undocumented_root_option() -> None:
    parser = build_parser()
    parser.add_argument("--version", action="version", version="test-version")
    rows = _marked_table(_reference_text(), "cli-options")

    with pytest.raises(AssertionError, match="CLI action contract drift"):
        _assert_cli_option_contract(rows, parser)


def test_cli_contract_catches_an_inherited_only_subcommand() -> None:
    parser = build_parser()
    _subparser_action(parser).add_parser(
        "audit",
        parents=[autograder_cli._parent_parser()],
    )
    rows = _marked_table(_reference_text(), "cli-options")

    with pytest.raises(AssertionError, match="CLI subcommand contract drift"):
        _assert_cli_option_contract(rows, parser)


def test_cli_contract_catches_changed_submissions_arity() -> None:
    parser = build_parser()
    grade_parser = _subparser_action(parser).choices["grade"]
    submissions = next(
        action for action in grade_parser._actions if action.dest == "submissions"
    )
    submissions.nargs = "*"
    rows = _marked_table(_reference_text(), "cli-options")

    with pytest.raises(AssertionError, match="CLI action contract drift"):
        _assert_cli_option_contract(rows, parser)


@pytest.mark.parametrize("destination", ["reasoning_effort", "provider_sort"])
def test_cli_contract_catches_changed_choices(destination: str) -> None:
    parser = build_parser()
    inspect_parser = _subparser_action(parser).choices["inspect"]
    action = next(
        action for action in inspect_parser._actions if action.dest == destination
    )
    assert action.choices is not None
    action.choices = (*action.choices, "legacy")
    rows = _marked_table(_reference_text(), "cli-options")

    with pytest.raises(AssertionError, match="CLI action contract drift"):
        _assert_cli_option_contract(rows, parser)


def test_public_guides_cover_openrouter_migration_contract() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in MAINTAINED_MARKDOWN)

    assert "OPENROUTER_API_KEY" in text
    assert "openrouter/auto-beta" in text
    assert "openai/gpt-5.1" in text
    assert "--allow-data-retention" in text
    assert "--allow-data-collection" in text
    assert "automatic prompt caching" in text.lower()
    assert "session" in text.lower() and "sticky" in text.lower()
    assert "schema 3" in text.lower()
    assert "fresh" in text.lower() and "--out" in text


def test_reference_run_binding_schema_matches_runtime() -> None:
    section = _reference_text().split("## Run binding and cache behavior", 1)[1]
    section = section.split("\n## ", 1)[0]
    documented_versions = re.findall(
        r"(?:schema version\s*\n|binding schema, currently )`(\d+)`",
        section,
    )
    runtime_version = str(RunBinding(assignment_sha256="test", config={}).schema_version)

    assert documented_versions == [runtime_version, runtime_version]


def test_getting_started_names_every_removed_interface_and_replacement() -> None:
    text = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")

    for removed in (
        "ANTHROPIC_API_KEY",
        "--thinking",
        "--effort",
        "--no-prompt-caching",
    ):
        assert removed in text
    for replacement in (
        "OPENROUTER_API_KEY",
        "--reasoning-effort",
        "automatic prompt caching",
    ):
        assert replacement in text


def test_reference_manifest_keys_match_the_written_artifact(tmp_path: Path) -> None:
    usage = {
        "api_calls": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "reasoning_tokens": 2,
        "cached_prompt_tokens": 3,
        "cache_write_tokens": 1,
        "cost_usd": 0.004,
        "resolved_models": ["vendor/model"],
        "providers": ["Provider One"],
    }
    path = tmp_path / "run_manifest.json"
    write_manifest(
        path,
        RunConfig(),
        {},
        [],
        usage,
        datetime(2026, 8, 9, tzinfo=timezone.utc),
        [],
        "complete",
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))

    assert _marked_json(_reference_text(), "manifest-key-contract") == {
        "top_level": sorted(manifest),
        "usage": sorted(manifest["usage"]),
    }


def test_runconfig_contract_rejects_a_duplicate_canonical_field_row() -> None:
    rows = [dict(row) for row in _runconfig_reference_rows()]
    rows.append(dict(rows[0]))

    with pytest.raises(AssertionError, match="duplicate RunConfig field"):
        _assert_runconfig_field_contract(rows)


def test_reference_cli_option_table_catches_complete_parser_action_drift() -> None:
    rows = _marked_table(_reference_text(), "cli-options")

    _assert_cli_option_contract(rows, build_parser())


def test_reference_runconfig_tables_catch_field_and_default_drift() -> None:
    rows = _runconfig_reference_rows()

    _assert_runconfig_field_contract(rows)


def test_reference_runconfig_tables_catch_cache_binding_drift() -> None:
    rows = _runconfig_reference_rows()
    documented = {
        _code_value(row["Field"])
        for row in rows
        if row["Run binding"] == "yes"
    }

    assert documented == set(RunConfig().cache_identity()), (
        "RunConfig cache-identity drift; update the structured Run binding cells"
    )


def test_reference_document_format_table_catches_ingestion_constant_drift() -> None:
    rows = _marked_table(_reference_text(), "document-formats")

    _assert_document_format_contract(rows)


def test_document_format_contract_rejects_a_stale_constant_name() -> None:
    rows = [dict(row) for row in _marked_table(_reference_text(), "document-formats")]
    for row in rows:
        row["Source constants"] = row["Source constants"].replace(
            "`SUPPORTED_EXTS`",
            "`SUPPORTED_EXTS`, `STALE_SUPPORTED_EXTS_SUFFIX`",
        )

    with pytest.raises(AssertionError, match="document-format constant drift"):
        _assert_document_format_contract(rows)


def test_reference_solution_json_example_matches_the_accepted_parser_shape(
    tmp_path: Path,
) -> None:
    reference = _reference_text()
    example = _marked_json(reference, "solution-json-example")
    key_path = tmp_path / "solutions.json"
    key_path.write_text(json.dumps(example), encoding="utf-8")

    solutions, issues = parse_provided_solutions(
        None,
        RunConfig(),
        AssignmentSpec(problems=[Problem(id="1")]),
        key_path,
        None,
        None,
    )

    assert issues == []
    assert set(solutions) == {"1"}
    assert solutions["1"].final_answer


def test_reference_rubric_json_example_matches_the_pydantic_input_shape() -> None:
    reference = _reference_text()
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
