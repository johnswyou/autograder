"""Offline tests for the OpenRouter chat seam, agent loop, and model helpers."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from autograder.config import DEFAULT_MODEL, PROVIDER_SORTS, REASONING_EFFORTS, RunConfig
from autograder.llm import (
    SUBMIT_TOOL_NAME,
    AgentError,
    AgentTask,
    ChatRequest,
    OpenRouterChatClient,
    ToolCall,
    Usage,
    UsageMeter,
    make_client,
    run_agent,
)
from autograder.models import AssignmentSpec
from autograder.solutions import dependency_levels

from .conftest import make_stub_client, text, tool_use, turn


class Tiny(BaseModel):
    answer: str
    score: float


def _task(**kw) -> AgentTask:
    base = {
        "name": "solver",
        "system": "sys",
        "user_content": [{"type": "text", "text": "go"}],
        "result_model": Tiny,
        "toolkit": None,
        "tool_names": (),
    }
    base.update(kw)
    return AgentTask(**base)


def test_default_model_and_reasoning_efforts_are_exact():
    assert DEFAULT_MODEL == "openrouter/auto-beta"
    assert REASONING_EFFORTS == ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    for effort in REASONING_EFFORTS:
        assert RunConfig(reasoning_effort=effort).reasoning_effort == effort
    with pytest.raises(ValueError, match="reasoning_effort must be one of"):
        RunConfig(reasoning_effort="extreme")


def test_provider_sorts_are_exact_and_validated():
    assert PROVIDER_SORTS == ("price", "throughput", "latency", "exacto")
    for sort in PROVIDER_SORTS:
        assert RunConfig(provider_sort=sort).provider_sort == sort
    assert RunConfig().provider_sort is None
    with pytest.raises(ValueError, match="provider_sort must be one of"):
        RunConfig(provider_sort="cheapest")


def test_cache_identity_uses_reasoning_and_privacy_not_anthropic_fields():
    identity = RunConfig(
        reasoning_effort="high",
        zero_data_retention=False,
        allow_data_collection=True,
    ).cache_identity()
    assert identity["reasoning_effort"] == "high"
    assert identity["zero_data_retention"] is False
    assert identity["allow_data_collection"] is True
    assert "thinking" not in identity
    assert "effort" not in identity
    assert "prompt_caching" not in identity


def test_cache_identity_ignores_provider_sort_so_a_directory_stays_reusable():
    default = RunConfig().cache_identity()
    sorted_run = RunConfig(provider_sort="throughput").cache_identity()

    assert "provider_sort" not in default
    assert sorted_run == default


def test_run_agent_submits_and_sends_exact_openrouter_policy(cfg: RunConfig):
    cfg.reasoning_effort = "high"
    client = make_stub_client([turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "42", "score": 0.9}))])
    meter = UsageMeter()

    result = run_agent(client, cfg, _task(), meter)

    assert result == Tiny(answer="42", score=0.9)
    request = client.calls[0]
    assert request.model == "openrouter/auto-beta"
    assert request.messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
    ]
    assert request.reasoning_effort == "high"
    assert request.provider == {
        "allow_fallbacks": True,
        "require_parameters": True,
        "zdr": True,
        "data_collection": "deny",
    }
    assert request.session_id
    assert request.tools[0]["type"] == "function"
    assert request.tools[0]["function"]["name"] == SUBMIT_TOOL_NAME
    assert meter.snapshot() == {
        "api_calls": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "reasoning_tokens": 0,
        "cached_prompt_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.0,
        "resolved_models": ["resolved/model"],
        "providers": ["Provider One"],
    }


def test_reasoning_effort_is_omitted_when_not_configured(cfg: RunConfig):
    client = make_stub_client([turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "a", "score": 1}))])
    run_agent(client, cfg, _task(), None)
    assert client.calls[0].reasoning_effort is None


def test_privacy_opt_outs_change_only_provider_privacy_values(cfg: RunConfig):
    cfg.zero_data_retention = False
    cfg.allow_data_collection = True
    client = make_stub_client([turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "a", "score": 1}))])
    run_agent(client, cfg, _task(), None)
    assert client.calls[0].provider == {
        "allow_fallbacks": True,
        "require_parameters": True,
        "zdr": False,
        "data_collection": "allow",
    }


def test_provider_sort_is_omitted_unless_configured(cfg: RunConfig):
    client = make_stub_client([turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "a", "score": 1}))])
    run_agent(client, cfg, _task(), None)
    assert "sort" not in client.calls[0].provider


def test_provider_sort_adds_only_the_sort_key_to_the_provider_policy(cfg: RunConfig):
    cfg.provider_sort = "throughput"
    client = make_stub_client([turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "a", "score": 1}))])
    run_agent(client, cfg, _task(), None)
    assert client.calls[0].provider == {
        "allow_fallbacks": True,
        "require_parameters": True,
        "zdr": True,
        "data_collection": "deny",
        "sort": "throughput",
    }


def test_session_id_is_nonempty_and_sticky_across_agent_turns(cfg: RunConfig):
    client = make_stub_client(
        [
            turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "x"})),
            turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "x", "score": 0.5})),
        ]
    )
    run_agent(client, cfg, _task(), None)
    assert len(client.calls[0].session_id) > 10
    assert client.calls[0].session_id == client.calls[1].session_id


def test_schema_repair_replays_assistant_and_uses_error_tool_message(cfg: RunConfig):
    first = turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "x"}))
    client = make_stub_client(
        [
            first,
            turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "x", "score": 0.5})),
        ]
    )
    assert run_agent(client, cfg, _task(), None).score == 0.5
    messages = client.calls[1].messages
    assert messages[2] == first.assistant_message
    assert messages[3]["role"] == "tool"
    assert messages[3]["tool_call_id"] == "tu_1"
    assert messages[3]["content"].startswith("ERROR: submission failed schema validation")


def test_malformed_tool_json_is_returned_as_error_and_can_be_repaired(cfg: RunConfig):
    client = make_stub_client(
        [
            turn(ToolCall(id="bad", name=SUBMIT_TOOL_NAME, arguments='{ "answer":')),
            turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "ok", "score": 1})),
        ]
    )
    assert run_agent(client, cfg, _task(), None).answer == "ok"
    response = client.calls[1].messages[3]
    assert response["role"] == "tool"
    assert response["tool_call_id"] == "bad"
    assert response["content"].startswith("ERROR: malformed JSON arguments")


def test_multiple_tool_calls_get_individual_tool_messages(cfg: RunConfig):
    client = make_stub_client(
        [
            turn(
                tool_use("missing", {}, id="one"),
                tool_use("also_missing", {}, id="two"),
            ),
            turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "done", "score": 1}, id="three")),
        ]
    )
    assert run_agent(client, cfg, _task(), None).answer == "done"
    assert client.calls[1].messages[3:5] == [
        {
            "role": "tool",
            "tool_call_id": "one",
            "content": "ERROR: no tools available, cannot run missing",
        },
        {
            "role": "tool",
            "tool_call_id": "two",
            "content": "ERROR: no tools available, cannot run also_missing",
        },
    ]


def test_result_check_rejection_is_fed_back_and_can_be_repaired(cfg: RunConfig):
    """A schema-valid result the caller still refuses re-enters the repair loop."""
    task = _task(result_check=lambda r: None if r.score >= 1 else "score must reach 1")
    client = make_stub_client(
        [
            turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "ok", "score": 0}, id="low")),
            turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "ok", "score": 1}, id="high")),
        ]
    )
    assert run_agent(client, cfg, task, None).score == 1
    complaint = client.calls[1].messages[-1]
    assert complaint["role"] == "tool"
    assert complaint["tool_call_id"] == "low"
    assert complaint["content"] == "ERROR: score must reach 1"


def test_result_check_gives_up_after_repeated_rejections(cfg: RunConfig):
    task = _task(result_check=lambda r: "never good enough")
    rejected = turn(tool_use(SUBMIT_TOOL_NAME, {"answer": "ok", "score": 0}))
    client = make_stub_client([rejected, rejected, rejected])
    with pytest.raises(AgentError, match="never good enough"):
        run_agent(client, cfg, task, None)
    assert len(client.calls) == 3


def test_two_nudges_then_error(cfg: RunConfig):
    client = make_stub_client([text("one"), text("two"), text("three")])
    with pytest.raises(AgentError, match=r"ended 3 turns.*finish_reason=stop"):
        run_agent(client, cfg, _task(), None)
    assert client.calls[1].messages[-1]["content"]
    assert client.calls[2].messages[-1]["content"]


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (turn(content="truncated", finish_reason="length"), "hit max_tokens"),
        (turn(content="blocked", finish_reason="content_filter", refusal="policy"), "content filter"),
        (turn(content="blocked", finish_reason="stop", refusal="policy"), "model refused"),
        (turn(error="provider disconnected"), "stream failed.*provider disconnected"),
    ],
)
def test_unrecoverable_turn_outcomes(response, match: str, cfg: RunConfig):
    with pytest.raises(AgentError, match=match):
        run_agent(make_stub_client([response]), cfg, _task(), None)


def test_turn_exhaustion(cfg: RunConfig):
    task = _task(max_turns=1)
    client = make_stub_client([turn(tool_use("missing", {}))])
    with pytest.raises(AgentError, match="exceeded 1 agent turns"):
        run_agent(client, cfg, task, None)


def _ns(**values):
    return SimpleNamespace(**values)


class _ReasoningDetail:
    def __init__(self, payload: dict):
        self.payload = payload

    def model_dump(self, **kwargs):
        assert kwargs == {"mode": "json", "exclude_none": True, "exclude_unset": True}
        return dict(self.payload)


class _FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __enter__(self):
        return iter(self.chunks)

    def __exit__(self, *exc):
        self.closed = True
        return False


class _FakeSDK:
    def __init__(self, chunks):
        self.stream = _FakeStream(chunks)
        self.sent = None
        self.send_calls = 0
        self.close_calls = 0
        self.chat = _ns(send=self.send)

    def send(self, **kwargs):
        self.send_calls += 1
        self.sent = kwargs
        return self.stream

    def __exit__(self, *exc):
        self.close_calls += 1


def _chunk(*, delta=None, finish=None, usage=None, model="resolved/model", metadata=None, error=None):
    choice = [] if delta is None else [_ns(delta=delta, finish_reason=finish, index=0)]
    return _ns(
        choices=choice,
        model=model,
        usage=usage,
        openrouter_metadata=metadata,
        error=error,
    )


@pytest.fixture
def future_reasoning_delta():
    from openrouter import components

    delta = components.ChatStreamDelta.model_validate(
        {
            "reasoning_details": [
                {
                    "type": "reasoning.future",
                    "id": "future-1",
                    "opaque": {"version": 2},
                }
            ],
            "tool_calls": [
                {
                    "index": 0,
                    "id": "local-1",
                    "type": "function",
                    "function": {"name": "compute", "arguments": '{"expression":"1+1"}'},
                }
            ],
        }
    )
    assert delta.reasoning_details is not None
    assert isinstance(delta.reasoning_details[0], components.UnknownReasoningDetailUnion)
    return delta


class _RecordingToolKit:
    def __init__(self):
        self.dispatched: list[tuple[str, dict]] = []

    def specs(self, names):
        return [
            {
                "type": "function",
                "function": {
                    "name": "compute",
                    "description": "Evaluate arithmetic.",
                    "parameters": {
                        "type": "object",
                        "properties": {"expression": {"type": "string"}},
                        "required": ["expression"],
                    },
                },
            }
        ]

    def dispatch(self, name, arguments):
        self.dispatched.append((name, arguments))
        return [{"type": "text", "text": "executed"}], False


def test_unknown_sdk_reasoning_detail_fails_before_tool_dispatch_or_replay(
    future_reasoning_delta, cfg: RunConfig
):
    sdk = _FakeSDK([_chunk(delta=future_reasoning_delta, finish="tool_calls")])
    toolkit = _RecordingToolKit()

    with pytest.raises(AgentError) as raised:
        run_agent(
            OpenRouterChatClient(sdk),
            cfg,
            _task(
                toolkit=toolkit,
                tool_names=("compute",),
                context="submission 7",
                max_turns=2,
            ),
            None,
        )

    cause = "OpenRouter SDK cannot safely replay unsupported reasoning_details variant 'reasoning.future'"
    assert str(raised.value) == f"[solver submission 7] OpenRouter call failed on turn 1: {cause}"
    assert isinstance(raised.value.__cause__, AgentError)
    assert str(raised.value.__cause__) == cause
    assert sdk.send_calls == 1
    assert toolkit.dispatched == []
    assert sdk.stream.closed


def test_openrouter_stream_assembly_preserves_reasoning_and_fragments():
    detail = {"type": "reasoning.text", "text": "secret", "id": "r1"}
    sdk = _FakeSDK(
        [
            _chunk(
                delta=_ns(
                    content="hel",
                    reasoning="rea",
                    refusal=None,
                    reasoning_details=[_ReasoningDetail(detail)],
                    tool_calls=[
                        _ns(
                            index=1,
                            id="b",
                            type="function",
                            function=_ns(name="submit_result", arguments='{"score":'),
                        )
                    ],
                )
            ),
            _chunk(
                delta=_ns(
                    content="lo",
                    reasoning="son",
                    refusal="no",
                    reasoning_details=None,
                    tool_calls=[
                        _ns(
                            index=1,
                            id=None,
                            type=None,
                            function=_ns(name="submit_result", arguments="1}"),
                        ),
                        _ns(index=0, id="a", type="function", function=_ns(name="other", arguments="{}")),
                    ],
                ),
                finish="tool_calls",
            ),
            _chunk(
                usage=_ns(
                    prompt_tokens=20,
                    completion_tokens=8,
                    cost=0.012,
                    completion_tokens_details=_ns(reasoning_tokens=3),
                    prompt_tokens_details=_ns(cached_tokens=4, cache_write_tokens=2),
                ),
                metadata=_ns(
                    attempts=[
                        _ns(model="failed/model", provider="Bad", status=500),
                        _ns(model="resolved/model", provider="Good", status=200),
                    ]
                ),
            ),
        ]
    )
    client = OpenRouterChatClient(sdk)
    request = ChatRequest(
        messages=[{"role": "user", "content": "go"}],
        tools=[],
        model="router",
        max_tokens=99,
        reasoning_effort="high",
        provider={"zdr": True},
        session_id="session",
    )

    response = client.complete(request)

    assert response.tool_calls == [
        ToolCall(id="a", name="other", arguments="{}"),
        ToolCall(id="b", name="submit_result", arguments='{"score":1}'),
    ]
    assert response.assistant_message == {
        "role": "assistant",
        "content": "hello",
        "refusal": "no",
        "reasoning": "reason",
        "reasoning_details": [detail],
        "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "other", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "submit_result", "arguments": '{"score":1}'}},
        ],
    }
    assert response.usage == Usage(
        prompt_tokens=20,
        completion_tokens=8,
        reasoning_tokens=3,
        cached_prompt_tokens=4,
        cache_write_tokens=2,
        cost_usd=0.012,
    )
    assert response.resolved_model == "resolved/model"
    assert response.provider == "Good"
    assert sdk.stream.closed
    assert sdk.sent == {
        "messages": request.messages,
        "tools": request.tools,
        "model": "router",
        "max_tokens": 99,
        "reasoning_effort": "high",
        "provider": {"zdr": True},
        "session_id": "session",
        "stream": True,
        "x_open_router_metadata": "enabled",
    }


def test_openrouter_omits_null_text_fields_but_preserves_empty_reasoning_details():
    sdk = _FakeSDK(
        [
            _chunk(
                delta=_ns(
                    content=None,
                    reasoning=None,
                    refusal=None,
                    reasoning_details=[],
                    tool_calls=[
                        _ns(
                            index=0,
                            id="call",
                            type="function",
                            function=_ns(name="submit_result", arguments="{}"),
                        )
                    ],
                ),
                finish="tool_calls",
            )
        ]
    )

    response = OpenRouterChatClient(sdk).complete(
        ChatRequest(
            messages=[],
            tools=[],
            model="m",
            max_tokens=1,
            reasoning_effort=None,
            provider={},
            session_id="s",
        )
    )

    assert response.assistant_message == {
        "role": "assistant",
        "reasoning_details": [],
        "tool_calls": [
            {
                "id": "call",
                "type": "function",
                "function": {"name": "submit_result", "arguments": "{}"},
            }
        ],
    }


def test_openrouter_in_band_error_discards_partial_call_and_closes_stream():
    sdk = _FakeSDK(
        [
            _chunk(
                delta=_ns(
                    content=None,
                    reasoning=None,
                    refusal=None,
                    reasoning_details=None,
                    tool_calls=[
                        _ns(
                            index=0,
                            id="partial",
                            type="function",
                            function=_ns(name="submit_result", arguments='{"answer":'),
                        )
                    ],
                )
            ),
            _chunk(error=_ns(code=502, message="provider disconnected")),
        ]
    )

    with pytest.raises(AgentError, match="OpenRouter stream error 502: provider disconnected"):
        OpenRouterChatClient(sdk).complete(
            ChatRequest(
                messages=[],
                tools=[],
                model="m",
                max_tokens=1,
                reasoning_effort=None,
                provider={},
                session_id="s",
            )
        )
    assert sdk.stream.closed


def test_openrouter_decoder_failure_is_contextual_and_closes_stream():
    def chunks():
        yield _chunk(
            delta=_ns(
                content="partial",
                reasoning=None,
                refusal=None,
                reasoning_details=None,
                tool_calls=None,
            )
        )
        raise ValueError("malformed event")

    sdk = _FakeSDK(chunks())
    with pytest.raises(AgentError, match="OpenRouter stream failed: malformed event"):
        OpenRouterChatClient(sdk).complete(
            ChatRequest(
                messages=[],
                tools=[],
                model="m",
                max_tokens=1,
                reasoning_effort=None,
                provider={},
                session_id="s",
            )
        )
    assert sdk.stream.closed


def test_openrouter_client_omits_optional_reasoning_and_closes_once():
    sdk = _FakeSDK(
        [
            _chunk(
                delta=_ns(content="done", reasoning=None, refusal=None, reasoning_details=None, tool_calls=None),
                finish="stop",
            )
        ]
    )
    client = OpenRouterChatClient(sdk)
    client.complete(
        ChatRequest(
            messages=[],
            tools=[],
            model="m",
            max_tokens=1,
            reasoning_effort=None,
            provider={},
            session_id="s",
        )
    )
    assert "reasoning_effort" not in sdk.sent
    client.close()
    client.close()
    assert sdk.close_calls == 1


def test_openrouter_client_wraps_pre_stream_failure():
    sdk = _FakeSDK([])
    sdk.chat.send = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
    client = OpenRouterChatClient(sdk)
    with pytest.raises(AgentError, match="OpenRouter request failed before streaming: offline"):
        client.complete(
            ChatRequest(
                messages=[],
                tools=[],
                model="m",
                max_tokens=1,
                reasoning_effort=None,
                provider={},
                session_id="s",
            )
        )


def test_installed_sdk_accepts_every_parameter_the_client_sends():
    from openrouter.chat import Chat

    sdk = _FakeSDK(
        [
            _chunk(
                delta=_ns(content="ok", reasoning=None, refusal=None, reasoning_details=None, tool_calls=None),
                finish="stop",
            )
        ]
    )
    request = ChatRequest(
        messages=[], tools=[], model="m", max_tokens=1, reasoning_effort="max", provider={}, session_id="s"
    )
    OpenRouterChatClient(sdk).complete(request)
    assert set(sdk.sent) <= set(inspect.signature(Chat.send).parameters)


def test_installed_sdk_validates_canonical_messages_tools_and_provider():
    from openrouter import components, utils

    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "go"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,eA=="},
                },
            ],
        },
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call",
                    "type": "function",
                    "function": {"name": "compute", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call",
            "content": [{"type": "text", "text": "accepted"}],
        },
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "compute",
                "description": "calculate",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    provider = {
        "allow_fallbacks": True,
        "require_parameters": True,
        "zdr": True,
        "data_collection": "deny",
    }
    for sort in PROVIDER_SORTS:
        parsed_sort = utils.get_pydantic_model(
            {**provider, "sort": sort}, components.ProviderPreferences
        )
        assert parsed_sort.sort == sort

    parsed_messages = utils.get_pydantic_model(messages, list[components.ChatMessages])
    parsed_tools = utils.get_pydantic_model(tools, list[components.ChatFunctionTool])
    parsed_provider = utils.get_pydantic_model(provider, components.ProviderPreferences)

    assert [message.role for message in parsed_messages] == ["system", "user", "assistant", "tool"]
    assert parsed_tools[0].function.name == "compute"
    assert parsed_provider.model_dump(exclude_unset=True) == provider


def test_make_client_sends_static_openrouter_attribution(monkeypatch):
    import openrouter

    captured = {}
    sdk = _FakeSDK([])

    def construct(**kwargs):
        captured.update(kwargs)
        return sdk

    monkeypatch.setattr(openrouter, "OpenRouter", construct)
    client = make_client(RunConfig(api_key="key"))
    assert captured == {
        "api_key": "key",
        "http_referer": "https://github.com/johnswyou/autograder",
        "x_open_router_title": "Agentic Autograder",
    }
    client.close()
    assert sdk.close_calls == 1


# -- models / scheduling ----------------------------------------------------


def test_result_models_reject_enveloped_payload():
    from pydantic import ValidationError

    from autograder.models import GradeDraft

    ok = AssignmentSpec.model_validate({"title": "HW", "problems": [{"id": "1", "type": "numeric"}]})
    assert [p.id for p in ok.leaves()] == ["1"]
    with pytest.raises(ValidationError):
        AssignmentSpec.model_validate({"result": {"title": "HW", "problems": []}})
    with pytest.raises(ValidationError):
        GradeDraft.model_validate({"result": {"criteria": [], "confidence": 0.5}})


def test_region_rotate_and_percentage_validation():
    from pydantic import ValidationError

    from autograder.models import Region

    assert Region(page=1, bbox=[0, 0, 50, 50], rotate=90).rotate == 90
    with pytest.raises(ValidationError):
        Region(page=1, bbox=[0, 0, 50, 50], rotate=45)
    with pytest.raises(ValidationError, match="percent"):
        Region(page=1, bbox=[340.0, 43.0, 980.0, 62.0])
    assert Region(page=1, bbox=[-0.4, 10.0, 100.4, 20.0]).bbox == [0.0, 10.0, 100.0, 20.0]


def test_spec_traversal_and_dependency_levels(small_spec: AssignmentSpec):
    assert small_spec.leaf_ids() == ["1a", "1b", "2"]
    assert [p.id for p in small_spec.stem_chain("1b")] == ["1", "1b"]
    assert dependency_levels(small_spec) == [["1a", "2"], ["1b"]]


def test_dependency_cycle_still_schedules_everything(small_spec: AssignmentSpec):
    small_spec.find("1a").depends_on = ["1b"]
    assert sorted(pid for level in dependency_levels(small_spec) for pid in level) == ["1a", "1b", "2"]


class _ProviderRefusal(Exception):
    """Shaped like ``openrouter.errors.OpenRouterError``.

    ``__str__`` yields only OpenRouter's short wrapper message, while the
    response body carries the provider's own explanation.
    """

    def __init__(self, message: str, body: str):
        super().__init__(message)
        self.message = message
        self.body = body

    def __str__(self) -> str:
        return self.message


_REFUSAL_BODY = (
    '{"error":{"code":400,"message":"Provider returned error","metadata":'
    '{"provider_name":"Google AI Studio","raw":"Invalid JSON payload received. '
    'Unknown name \\"$defs\\" at tools[0].function_declarations[0].parameters"}}}'
)


def _refusing_client(where: str) -> OpenRouterChatClient:
    sdk = _FakeSDK([])
    error = _ProviderRefusal("Provider returned error", _REFUSAL_BODY)

    def raise_it(**kwargs):
        raise error

    def raise_on_iter(**kwargs):
        raise error

    if where == "send":
        sdk.chat.send = raise_it
    else:
        sdk.chat.send = lambda **kwargs: _RaisingStream(error)
    return OpenRouterChatClient(sdk)


class _RaisingStream:
    def __init__(self, error: Exception):
        self._error = error

    def __enter__(self):
        raise self._error

    def __exit__(self, *exc_info):
        return False


def _bare_request() -> ChatRequest:
    return ChatRequest(
        messages=[], tools=[], model="m", max_tokens=1,
        reasoning_effort=None, provider={}, session_id="s",
    )


def test_a_pre_stream_provider_refusal_reports_what_the_provider_said():
    """OpenRouter answers 'Provider returned error' for anything upstream
    refused. Without the body, an operator cannot tell an unusable tool schema
    from a transient fault, and has to open the OpenRouter dashboard to find
    out which one cost them the run."""
    with pytest.raises(AgentError) as caught:
        _refusing_client("send").complete(_bare_request())

    assert "Provider returned error" in str(caught.value)
    assert "Google AI Studio" in str(caught.value)
    assert "$defs" in str(caught.value)


def test_a_mid_stream_provider_refusal_reports_what_the_provider_said():
    with pytest.raises(AgentError) as caught:
        _refusing_client("stream").complete(_bare_request())

    assert "Google AI Studio" in str(caught.value)


def test_an_error_carrying_no_body_is_reported_unchanged():
    sdk = _FakeSDK([])
    sdk.chat.send = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
    with pytest.raises(AgentError, match=r"before streaming: offline$"):
        OpenRouterChatClient(sdk).complete(_bare_request())


# -- routing provenance -----------------------------------------------------


def test_the_selected_endpoint_names_the_provider_that_served_the_turn():
    """The live metadata reports routing under ``endpoints.available``, marking
    the chosen one ``selected``; ``attempts`` is present only when the router
    had to retry. Reading ``attempts`` alone left ``provider`` unset on every
    ordinary call, so ``run_manifest.json`` recorded no provider at all — the
    one fact that distinguishes a run served entirely by one endpoint from a
    run whose turns were split across two.
    """
    sdk = _FakeSDK([
        _chunk(delta=_ns(content="ok", reasoning=None, refusal=None,
                         reasoning_details=None, tool_calls=None), finish="stop"),
        _chunk(metadata=_ns(
            attempts=None,
            endpoints=_ns(total=2, available=[
                _ns(model="google/gemini-3.7-flash-20260813", provider="Google AI Studio",
                    selected=False),
                _ns(model="google/gemini-3.7-flash-20260813", provider="Google", selected=True),
            ]),
        )),
    ])

    response = OpenRouterChatClient(sdk).complete(_bare_request())

    assert response.provider == "Google"
    assert response.resolved_model == "google/gemini-3.7-flash-20260813"


def test_a_retry_chain_still_names_the_endpoint_that_answered():
    """When the router did retry, the successful attempt is authoritative."""
    sdk = _FakeSDK([
        _chunk(delta=_ns(content="ok", reasoning=None, refusal=None,
                         reasoning_details=None, tool_calls=None), finish="stop"),
        _chunk(metadata=_ns(
            attempts=[
                _ns(model="failed/model", provider="Bad", status=500),
                _ns(model="resolved/model", provider="Good", status=200),
            ],
            endpoints=_ns(total=1, available=[
                _ns(model="resolved/model", provider="Good", selected=True),
            ]),
        )),
    ])

    response = OpenRouterChatClient(sdk).complete(_bare_request())

    assert response.provider == "Good"


# -- pinning the endpoints a run may reach ----------------------------------


def test_no_allowlist_leaves_the_routing_policy_untouched():
    from autograder.config import RunConfig
    from autograder.llm import _provider_policy

    assert "only" not in _provider_policy(RunConfig(model="m"))


def test_an_allowlist_reaches_the_provider_policy_as_only():
    """`only` is OpenRouter's allowlist of provider slugs. Fallbacks stay on:
    the point is to keep the router inside the allowed set, not to forbid it
    from trying a second endpoint within that set."""
    from autograder.config import RunConfig
    from autograder.llm import _provider_policy

    policy = _provider_policy(RunConfig(model="m", provider_only=("google-ai-studio",)))

    assert policy["only"] == ["google-ai-studio"]
    assert policy["allow_fallbacks"] is True


def test_the_allowlist_binds_the_output_directory():
    """Unlike a sort order, an allowlist decides which companies were permitted
    to process the submissions. That is the same kind of statement as the two
    privacy flags beside it, so artifacts made under one allowlist must not be
    silently reused under another."""
    from autograder.config import RunConfig

    wide = RunConfig(model="m").cache_identity()
    narrow = RunConfig(model="m", provider_only=("google-ai-studio",)).cache_identity()

    assert wide["provider_only"] == []
    assert narrow["provider_only"] == ["google-ai-studio"]
    assert wide != narrow


def test_a_blank_provider_slug_is_rejected():
    from autograder.config import RunConfig

    with pytest.raises(ValueError, match="provider_only"):
        RunConfig(model="m", provider_only=("google-ai-studio", "  ")).validate_limits()
