"""Shared fixtures: tiny PDF documents, a small assignment spec, a stub client."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from autograder.config import RunConfig
from autograder.models import AssignmentSpec, Problem, ProblemType


@pytest.fixture()
def tiny_pdf(tmp_path: Path) -> Path:
    """Two-page PDF with a real text layer."""
    doc = pymupdf.open()
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((72, 100), "Problem 1: compute 2 + 2.", fontsize=12)
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((72, 100), "Problem 2: sketch y = x^2.", fontsize=12)
    path = tmp_path / "tiny.pdf"
    doc.save(path)
    doc.close()
    return path


@pytest.fixture()
def cfg() -> RunConfig:
    return RunConfig(api_key="test-key", max_workers=2)


@pytest.fixture()
def small_spec() -> AssignmentSpec:
    return AssignmentSpec(
        title="Quiz",
        total_points=10.0,
        n_pages=2,
        problems=[
            Problem(id="1", label="Problem 1", prompt="Shared stem.", type=ProblemType.container,
                    children=[
                        Problem(id="1a", label="(a)", prompt="Find t.", type=ProblemType.numeric,
                                points=3.0, pages=[1]),
                        Problem(id="1b", label="(b)", prompt="Find v using (a).",
                                type=ProblemType.numeric, points=3.0, pages=[1],
                                depends_on=["1a"]),
                    ]),
            Problem(id="2", label="Problem 2", prompt="Pick one.", type=ProblemType.multiple_choice,
                    points=4.0, pages=[2], choices=["zero", "g down"]),
        ],
    )


class _StreamCtx:
    """Mimics anthropic's MessageStreamManager context manager.

    The real loop uses ``with client.messages.stream(**params) as s:`` then
    ``s.get_final_message()``; this reproduces that surface so tests exercise
    the streaming path exactly as production does.
    """

    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return SimpleNamespace(get_final_message=lambda: self._message)

    def __exit__(self, *exc):
        return False


def make_stub_client(script):
    """A stand-in for anthropic.Anthropic.

    ``script`` is a list of responses; each response is a list of content
    blocks (SimpleNamespace with .type, plus .name/.id/.input for tool_use),
    or an Exception instance to make that call raise (simulating an API
    failure after SDK retries). Each messages.stream() call pops the next
    response. Only ``stream`` is exposed (no ``create``) so any regression
    back to the non-streaming call fails loudly here.
    """
    calls: list[dict] = []
    queue = list(script)

    def stream(**params):
        calls.append(params)
        if not queue:
            raise AssertionError("stub client ran out of scripted responses")
        content = queue.pop(0)
        if isinstance(content, Exception):
            raise content
        message = SimpleNamespace(
            content=content,
            stop_reason="tool_use" if any(getattr(b, "type", "") == "tool_use" for b in content) else "end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  cache_creation_input_tokens=3, cache_read_input_tokens=7),
        )
        return _StreamCtx(message)

    client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    client.calls = calls
    return client


def tool_use(name: str, input: dict, id: str = "tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=input, id=id)


def text(t: str):
    return SimpleNamespace(type="text", text=t)
