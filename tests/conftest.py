"""Shared fixtures: tiny PDF documents, a small assignment spec, a stub client."""

from __future__ import annotations

import copy
from pathlib import Path

import pymupdf
import pytest

from autograder.config import RunConfig
from autograder.llm import ChatRequest, ChatTurn, ToolCall, Usage
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


class ScriptedChatClient:
    """Offline ChatClient that records immutable request snapshots."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[ChatRequest] = []
        self.close_calls = 0

    def complete(self, request: ChatRequest) -> ChatTurn:
        self.calls.append(copy.deepcopy(request))
        if not self._script:
            raise AssertionError("scripted client ran out of responses")
        response = self._script.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, list):
            if len(response) == 1 and isinstance(response[0], ChatTurn):
                return response[0]
            return turn(*response)
        return response

    def close(self) -> None:
        self.close_calls += 1


def make_stub_client(script):
    return ScriptedChatClient(script)


def turn(
    *tool_calls: ToolCall,
    content: str = "",
    finish_reason: str | None = None,
    refusal: str | None = None,
    error: str | None = None,
    usage: Usage | None = None,
    model: str = "resolved/model",
    provider: str = "Provider One",
) -> ChatTurn:
    calls = list(tool_calls)
    message: dict = {"role": "assistant", "content": content or None}
    if calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in calls
        ]
    if refusal is not None:
        message["refusal"] = refusal
    return ChatTurn(
        assistant_message=message,
        tool_calls=calls,
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
        refusal=refusal,
        error=error,
        usage=usage or Usage(prompt_tokens=10, completion_tokens=5),
        resolved_model=model,
        provider=provider,
    )


def tool_use(name: str, input: dict | str, id: str = "tu_1") -> ToolCall:
    import json

    arguments = input if isinstance(input, str) else json.dumps(input)
    return ToolCall(id=id, name=name, arguments=arguments)


def text(t: str) -> ChatTurn:
    return turn(content=t)
