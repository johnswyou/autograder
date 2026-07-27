"""Offline tests for the agent loop (stub client), models, and scheduling."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from autograder.config import RunConfig
from autograder.llm import SUBMIT_TOOL_NAME, AgentError, AgentTask, UsageMeter, run_agent
from autograder.models import AssignmentSpec
from autograder.solutions import dependency_levels

from .conftest import make_stub_client, text, tool_use


class Tiny(BaseModel):
    answer: str
    score: float


def _task(**kw) -> AgentTask:
    base = {"name": "solver", "system": "sys", "user_content": [{"type": "text", "text": "go"}],
            "result_model": Tiny, "toolkit": None, "tool_names": ()}
    base.update(kw)
    return AgentTask(**base)


def test_run_agent_happy_path(cfg: RunConfig):
    client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, {"answer": "42", "score": 0.9})],
    ])
    meter = UsageMeter()
    out = run_agent(client, cfg, _task(), meter)
    assert isinstance(out, Tiny) and out.answer == "42"
    assert meter.snapshot()["api_calls"] == 1
    # adaptive thinking requested for every agent under 'on'
    assert client.calls[0]["thinking"] == {"type": "adaptive"}
    # submit_result schema came from the pydantic model
    names = [t["name"] for t in client.calls[0]["tools"]]
    assert names == [SUBMIT_TOOL_NAME]


def test_run_agent_validation_repair(cfg: RunConfig):
    client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, {"answer": "x"})],                 # missing score -> error fed back
        [tool_use(SUBMIT_TOOL_NAME, {"answer": "x", "score": 0.5})],   # repaired
    ])
    out = run_agent(client, cfg, _task(), None)
    assert out.score == 0.5
    # the loop fed a tool_result error back before the repaired second call
    msgs = client.calls[1]["messages"]  # same list object, mutated in place by the loop
    tr = msgs[2]["content"][0]          # [user, assistant1, tool_result, assistant2]
    assert tr["type"] == "tool_result" and tr.get("is_error")
    assert "failed schema validation" in tr["content"][0]["text"]


def test_run_agent_nudge_then_fail(cfg: RunConfig):
    client = make_stub_client([
        [text("rambling")], [text("more rambling")], [text("still no tool")],
    ])
    with pytest.raises(AgentError, match=r"stop_reason=end_turn"):
        run_agent(client, cfg, _task(), None)


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("refusal", "declined this request"),
        ("model_context_window_exceeded", "exhausted the model's context window"),
    ],
)
def test_run_agent_reports_unrecoverable_stop_reasons_without_nudging(
    cfg: RunConfig, stop_reason: str, expected: str,
):
    """A refusal or an exhausted context window cannot be nudged away. The agent
    must stop on the first turn and record the real cause, because that message
    is what reaches grades.json and the review queue."""
    client = make_stub_client([[text("no tool call")]])
    client.calls.clear()

    def stream(**params):
        client.calls.append(params)
        return _StopReasonCtx(stop_reason)

    client.messages.stream = stream

    with pytest.raises(AgentError, match=expected):
        run_agent(client, cfg, _task(), None)
    assert len(client.calls) == 1, "must not spend nudge turns on an unrecoverable stop"


class _StopReasonCtx:
    """Minimal stand-in for a response that stops without any tool call."""

    def __init__(self, stop_reason: str) -> None:
        self._stop_reason = stop_reason

    def __enter__(self):
        from types import SimpleNamespace

        message = SimpleNamespace(content=[], stop_reason=self._stop_reason, usage=None)
        return SimpleNamespace(get_final_message=lambda: message)

    def __exit__(self, *exc):
        return False


def test_thinking_modes(cfg: RunConfig):
    client = make_stub_client([[tool_use(SUBMIT_TOOL_NAME, {"answer": "a", "score": 1})]])
    cfg.thinking = "off"
    run_agent(client, cfg, _task(), None)
    assert client.calls[0]["thinking"] == {"type": "disabled"}

    client = make_stub_client([[tool_use(SUBMIT_TOOL_NAME, {"answer": "a", "score": 1})]])
    cfg.thinking = "on"
    cfg.effort = "high"
    run_agent(client, cfg, _task(name="mapper"), None)
    assert client.calls[0]["thinking"] == {"type": "adaptive"}
    assert client.calls[0]["output_config"] == {"effort": "high"}


def test_installed_sdk_accepts_every_parameter_the_agent_loop_sends(cfg: RunConfig):
    """Guard the dependency floor for `anthropic`.

    Every other test stubs the client, so the real SDK's request surface is
    never exercised and an `anthropic` release too old to accept one of these
    parameters would pass the whole suite — then fail on the first live call.
    The parameter set is captured from `run_agent` rather than hard-coded, so
    this keeps working when the loop starts sending something new.
    """
    import inspect

    import anthropic

    cfg.effort = "high"  # exercises the optional output_config branch
    client = make_stub_client([[tool_use(SUBMIT_TOOL_NAME, {"answer": "a", "score": 1})]])
    run_agent(client, cfg, _task(), None)
    sent = set(client.calls[0])

    accepted = set(
        inspect.signature(anthropic.resources.messages.Messages.stream).parameters
    )
    unsupported = sorted(sent - accepted)

    assert not unsupported, (
        f"installed anthropic {anthropic.__version__} does not accept "
        f"{unsupported}; raise the floor in pyproject.toml"
    )


@pytest.mark.parametrize("removed_mode", ["auto", "all"])
def test_run_config_rejects_removed_thinking_modes(removed_mode: str):
    with pytest.raises(ValueError, match="thinking must be one of: on, off"):
        RunConfig(thinking=removed_mode)


def test_run_config_rejects_thinking_off_on_always_reasoning_models():
    with pytest.raises(ValueError, match="always reasons"):
        RunConfig(model="claude-fable-5", thinking="off")
    # The same model is fine with thinking left on.
    assert RunConfig(model="claude-fable-5", thinking="on").effort is None


def test_run_config_rejects_thinking_off_above_capped_effort():
    with pytest.raises(ValueError, match="above effort 'high'"):
        RunConfig(model="claude-opus-5", thinking="off", effort="xhigh")
    # At or below the cap the combination is accepted.
    assert RunConfig(model="claude-opus-5", thinking="off", effort="high").effort == "high"
    # An unlisted model is never pre-checked.
    assert RunConfig(model="some-future-model", thinking="off", effort="max").thinking == "off"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("solution_max_rounds", -1, "zero or greater"),
        ("max_workers", 0, "positive integer"),
        ("max_agent_turns", -5, "positive integer"),
        ("max_tool_images", -1, "zero or greater"),
        ("max_upscale", 0.5, "at least 1.0"),
        ("review_confidence", 1.5, "between 0 and 1"),
        ("effort", "extreme", "effort must be one of"),
    ],
)
def test_run_config_range_checks_programmatic_overrides(field, value, expected):
    """The architecture guide advertises these as programmatic overrides, so an
    out-of-range value must fail at construction rather than part-way through a
    paid run."""
    with pytest.raises(ValueError, match=expected):
        RunConfig(**{field: value})


@pytest.mark.parametrize("removed_mode", ["auto", "all"])
def test_run_agent_rejects_removed_thinking_modes(cfg: RunConfig, removed_mode: str):
    cfg.thinking = removed_mode
    client = make_stub_client([[tool_use(SUBMIT_TOOL_NAME, {"answer": "a", "score": 1})]])
    with pytest.raises(ValueError, match="thinking must be one of: on, off"):
        run_agent(client, cfg, _task(), None)
    assert client.calls == []


def test_result_models_reject_enveloped_payload():
    """A submission wrapped in an envelope ({"result": {...}}) or with stray keys must
    RAISE, not silently validate as an all-defaults empty object — otherwise the agent
    loop's schema-repair never fires. Regression for the Sonnet-5 spec-wrapping bug."""
    from pydantic import ValidationError

    from autograder.models import GradeDraft

    ok = AssignmentSpec.model_validate({"title": "HW", "problems": [{"id": "1", "type": "numeric"}]})
    assert [p.id for p in ok.leaves()] == ["1"]
    with pytest.raises(ValidationError):
        AssignmentSpec.model_validate({"result": {"title": "HW", "problems": []}})
    with pytest.raises(ValidationError):
        GradeDraft.model_validate({"result": {"criteria": [], "confidence": 0.5}})


def test_run_agent_repairs_enveloped_submission(cfg: RunConfig):
    """An agent that wraps submit_result in {"result": {...}} must be told its output
    failed validation and repair it — not have it silently accepted as an empty spec."""
    client = make_stub_client([
        [tool_use(SUBMIT_TOOL_NAME, {"result": {"title": "HW", "problems": [{"id": "1", "type": "numeric"}]}})],
        [tool_use(SUBMIT_TOOL_NAME, {"title": "HW", "problems": [{"id": "1", "type": "numeric"}]})],
    ])
    out = run_agent(client, cfg, _task(name="spec", result_model=AssignmentSpec), None)
    assert isinstance(out, AssignmentSpec) and [p.id for p in out.leaves()] == ["1"]
    tr = client.calls[1]["messages"][2]["content"][0]   # error fed back before the repaired call
    assert tr.get("is_error") and "schema validation" in tr["content"][0]["text"]


# -- models / scheduling -------------------------------------------------------


def test_region_rotate_field():
    from pydantic import ValidationError

    from autograder.models import Region

    assert Region(page=1, bbox=[0, 0, 50, 50]).rotate == 0
    assert Region(page=1, bbox=[0, 0, 50, 50], rotate=90).rotate == 90
    with pytest.raises(ValidationError):
        Region(page=1, bbox=[0, 0, 50, 50], rotate=45)


def test_region_rejects_coordinates_that_are_not_percentages():
    """Clamping a pixel-valued bbox into range silently produces a degenerate
    sliver that renders blank, and the agent is never told. Raising instead sends
    the mistake back through the submit_result repair loop, where it is fixable."""
    from pydantic import ValidationError

    from autograder.models import Region

    with pytest.raises(ValidationError) as excinfo:
        Region(page=1, bbox=[340.0, 43.0, 980.0, 62.0])
    assert "percent" in str(excinfo.value).lower()


def test_region_still_absorbs_rounding_slop():
    """A model writing 100.4 for 'the right edge' means the right edge."""
    from autograder.models import Region

    assert Region(page=1, bbox=[-0.4, 10.0, 100.4, 20.0]).bbox == [0.0, 10.0, 100.0, 20.0]


def test_spec_traversal(small_spec: AssignmentSpec):
    assert small_spec.leaf_ids() == ["1a", "1b", "2"]
    chain = [p.id for p in small_spec.stem_chain("1b")]
    assert chain == ["1", "1b"]
    stem = small_spec.stem_text("1b")
    assert "Shared stem." in stem and "Find v" in stem


def test_dependency_levels(small_spec: AssignmentSpec):
    levels = dependency_levels(small_spec)
    assert levels == [["1a", "2"], ["1b"]]


def test_dependency_cycle(small_spec: AssignmentSpec):
    small_spec.find("1a").depends_on = ["1b"]  # 1a <-> 1b cycle
    levels = dependency_levels(small_spec)
    flat = [pid for lvl in levels for pid in lvl]
    assert sorted(flat) == ["1a", "1b", "2"]  # everything still scheduled once
