"""Focused structural guards for maintained documentation."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from autograder import __version__

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


def test_local_link_parser_includes_line_start_links_and_excludes_images() -> None:
    targets = _local_link_targets("[Guide](reference.md)\n![Diagram](diagram.png)")

    assert targets == ["reference.md"]


def test_required_documentation_set_exists() -> None:
    missing = [
        path.relative_to(ROOT).as_posix()
        for path in MAINTAINED_MARKDOWN
        if not path.is_file()
    ]

    assert not missing, f"missing maintained documentation: {', '.join(missing)}"


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
