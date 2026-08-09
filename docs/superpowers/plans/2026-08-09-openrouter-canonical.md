# OpenRouter Canonical LLM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Anthropic-only transport with OpenRouter as the repository's canonical and default way to select and call LLMs.

**Architecture:** Preserve the stage-facing `AgentTask`/`run_agent` deep interface. Inside `autograder.llm`, introduce a narrow `ChatClient.complete(ChatRequest) -> ChatTurn` seam whose production implementation streams OpenRouter Chat Completions and whose offline implementation is scripted by tests.

**Tech Stack:** Python 3.10+, `openrouter>=1.0.3`, Pydantic 2, pytest, Ruff 0.16.0, mypy 2.3.0.

## Global Constraints

- OpenRouter Chat Completions is the only production LLM protocol; do not retain an Anthropic compatibility adapter or add a generic provider registry.
- The default model is exactly `openrouter/auto-beta`; any OpenRouter model slug is accepted without provider-specific validation.
- Credentials come only from `OPENROUTER_API_KEY` or `--api-key`; never read `ANTHROPIC_API_KEY`.
- Provider requests always set `allow_fallbacks=true` and `require_parameters=true`; defaults also set `zdr=true` and `data_collection="deny"`.
- The supported reasoning efforts are exactly `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`; omission uses the model default.
- Preserve `reasoning_details` exactly when replaying an assistant tool-call message.
- Do not emit `cache_control` or retain `--no-prompt-caching`.
- All automated tests are offline; no test may require a real API key or network call.
- Existing successful stage interfaces and artifact schemas remain unchanged except run binding schema 3 and expanded manifest configuration/usage fields.

---

### Task 1: Make OpenRouter the canonical repository workflow

This migration is one task because configuration construction, the message
protocol, the agent loop, CLI parsing, persistence, tests, and public examples
form one compatibility boundary. Splitting them would leave intermediate
commits whose CLI constructs a configuration the runtime cannot consume or
whose tests assert two contradictory protocols.

**Files:**
- Modify: `pyproject.toml`
- Modify: `autograder/config.py`
- Modify: `autograder/tools.py`
- Modify: `autograder/llm.py`
- Modify: `autograder/cli.py`
- Modify: `autograder/orchestrator.py`
- Modify: `autograder/report.py`
- Modify: `autograder/run_state.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_llm_models.py`
- Modify: `tests/test_caching_and_staleness.py`
- Modify: `tests/test_pipeline_units.py`
- Modify: `tests/test_run_state.py`
- Modify: `tests/test_failures_and_resume.py`
- Modify: `tests/test_documentation.py`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `docs/README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/getting-started.md`
- Modify: `docs/how-it-works.md`
- Modify: `docs/reference.md`
- Modify: `docs/usage.md`
- Modify: any other maintained file that contains an Anthropic-only contract

**Interfaces:**
- Produces: `ReasoningEffort`, `REASONING_EFFORTS`, and `RunConfig.reasoning_effort: ReasoningEffort | None`.
- Produces: `RunConfig.zero_data_retention: bool` and `RunConfig.allow_data_collection: bool`.
- Produces: OpenRouter `text` and `image_url` content blocks plus function-tool dictionaries from `ToolKit.specs()`.
- Produces: `ChatRequest`, `ChatTurn`, `ToolCall`, `Usage`, and `ChatClient` within `autograder.llm`.
- Produces: `OpenRouterChatClient.complete(request: ChatRequest) -> ChatTurn` and idempotent `close()`.
- Preserves: `make_client(cfg)`, `run_agent(client, cfg, task, meter)`, `AgentTask`, `UsageMeter`, and `AgentError` for existing callers.
- CLI produces `RunConfig` from `--reasoning-effort`, `--allow-data-retention`, `--allow-data-collection`, and `OPENROUTER_API_KEY`.
- `UsageMeter.snapshot()` produces `api_calls`, `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_prompt_tokens`, `cache_write_tokens`, `cost_usd`, `resolved_models`, and `providers`.
- `RunBinding.schema_version` is exactly 3.

- [ ] **Step 1: Write failing core protocol tests**

  Replace the Anthropic stub with a scripted `ChatClient` fixture that records
  immutable request snapshots and returns complete `ChatTurn` values. Add
  focused tests for:

  - the exact default model and accepted/rejected reasoning efforts;
  - the reasoning/privacy fields in `cache_identity()` and removal of old
    thinking/cache fields;
  - base64 `image_url` content, function-tool schemas, and eviction of old
    images from individual `role: tool` messages;
  - valid submission, schema repair, malformed tool JSON, multiple tool
    responses, two nudge attempts, `length`, `content_filter`, mid-stream
    errors, and turn exhaustion;
  - one nonempty session ID reused through a single agent loop;
  - optional reasoning effort, exact provider policy, and normalized usage.

  Hand-write literal expected values; do not derive expectations with the code
  under test.

- [ ] **Step 2: Write failing OpenRouter SDK boundary tests**

  Give `OpenRouterChatClient` a fake SDK whose `chat.send(stream=True)` returns
  SDK-shaped chunk objects. Cover fragmented content and function arguments by
  tool-call index, exact `reasoning_details` preservation, refusal text, final
  usage, resolved `model`, provider metadata, stream closure, client closure,
  and contextual pre-stream failure. Compare captured request keys with
  `inspect.signature(openrouter.chat.Chat.send)` so the declared SDK floor is
  truthful.

- [ ] **Step 3: Write failing runtime and persistence tests**

  Add CLI, pipeline, binding, and manifest tests proving:

  - `OPENROUTER_API_KEY` and the three new options map to `RunConfig`;
  - `--thinking`, `--effort`, and `--no-prompt-caching` are rejected;
  - the no-key warning and logger namespace are OpenRouter-specific;
  - cached-only pipelines stay lazy, while a created client closes exactly once
    on successful and exceptional entry-point exits;
  - schema-2 bindings are rejected and schema-3 config changes name the
    reasoning/privacy field that differs;
  - manifests separate requested model, resolved models, and providers and
    include normalized usage/cost without credentials or message content.

- [ ] **Step 4: Run each focused group and verify RED**

  Run:

  ```bash
  python -m pytest tests/test_llm_models.py tests/test_caching_and_staleness.py -q
  python -m pytest tests/test_pipeline_units.py tests/test_run_state.py tests/test_failures_and_resume.py -q
  ```

  Confirm failures are caused by missing OpenRouter behavior or stale
  Anthropic shapes, not test syntax or fixture errors.

- [ ] **Step 5: Implement configuration and canonical content shapes**

  Replace `anthropic>=0.77` with `openrouter>=1.0.3` and update the project
  description. Set `DEFAULT_MODEL = "openrouter/auto-beta"`; remove
  `ThinkingMode`, Claude-family tables, `thinking`, `effort`,
  `prompt_caching`, and their validators. Add the three new configuration
  fields and bind them in `cache_identity()`.

  Encode each JPEG as an `image_url` content part whose URL begins exactly
  `data:image/jpeg;base64,`. Convert existing local schemas from
  `name`/`description`/`input_schema` to `type="function"` with
  `function.name`, `function.description`, and `function.parameters`. Update
  stale-image eviction for `role: tool` content lists.

- [ ] **Step 6: Implement the chat seam and streamed agent loop**

  Instantiate `OpenRouter` with the configured key, repository URL as
  `http_referer`, and `Agentic Autograder` as `x_open_router_title`. Call
  `chat.send` with `stream=True`, `x_open_router_metadata="enabled"`,
  `max_completion_tokens`, function tools, a per-agent session ID, provider
  values from the Global Constraints, and `reasoning_effort` only when set.
  Consume the returned stream as a context manager.

  Accumulate text, refusal, reasoning, and fragmented tool calls. Serialize SDK
  reasoning-detail models with
  `model_dump(mode="json", exclude_none=True, exclude_unset=True)` and replay
  the resulting assistant message unchanged. Normalize usage and successful
  routing attempts without retaining SDK objects in `ChatTurn`.

  Rebuild `run_agent` over `ChatRequest`/`ChatTurn`: start with system and user
  roles, parse each tool call's JSON arguments, and append one `role: tool`
  response per call. Prefix malformed arguments, local tool failures, and
  Pydantic validation failures with `ERROR:`. Preserve valid submission,
  nudges, turn cap, image eviction, contextual errors, and the generic result
  return type.

- [ ] **Step 7: Implement CLI, lifecycle, binding, and manifest migration**

  Replace the environment lookup, option help, warning, and logger namespace.
  Remove the three Anthropic-era options and add the new reasoning/privacy
  options. Keep client creation lazy and make `Pipeline.close()` idempotently
  close both a created chat client and the assignment document.

  Set the run-binding literal/version to 3. Record requested model,
  resolved-model/provider sets, reasoning/privacy configuration, normalized
  token fields, and OpenRouter cost. Never record the API key, messages, or tool
  payloads. Update final usage logging to the normalized field names.

- [ ] **Step 8: Run focused implementation tests and verify GREEN**

  Run both commands from Step 4, then:

  ```bash
  ruff check autograder/ scripts/ tests/
  mypy autograder/ scripts/
  ```

  Fix implementation defects without weakening behavioral assertions.

- [ ] **Step 9: Commit the runtime migration**

  Commit production code and its behavioral tests as
  `refactor: make OpenRouter the canonical LLM transport`.

- [ ] **Step 10: Write failing public-contract tests and verify RED**

  Replace Anthropic-era documentation assertions with executable CLI/help and
  example checks for `OPENROUTER_API_KEY`, `openrouter/auto-beta`, a fixed
  OpenRouter model slug, the privacy opt-outs, and the fresh-output-directory
  migration. Prefer observable parser/help behavior over grepping prose.

  Run `python -m pytest tests/test_documentation.py -q` and confirm failures
  identify stale public instructions.

- [ ] **Step 11: Rewrite documentation and remove stale infrastructure**

  Document key setup, the dynamic default, fixed-slug reproducibility,
  automatic caching and session stickiness, privacy routing and opt-outs,
  usage/cost audit fields, removed Anthropic options/environment variable, and
  the schema-3 fresh-directory requirement.

  Run:

  ```bash
  rg -n -i 'anthropic|claude-sonnet-5|ANTHROPIC_API_KEY|prompt_caching|cache_control|--thinking|--effort' autograder tests README.md SECURITY.md docs pyproject.toml
  ```

  Remove every production or public Anthropic-only reference. An
  `anthropic/claude-*` example is permitted only when it is explicitly an
  ordinary OpenRouter model slug and no code special-cases it.

- [ ] **Step 12: Commit documentation and contract tests**

  Run `python -m pytest tests/test_documentation.py -q`, then commit as
  `docs: make OpenRouter the canonical workflow`.

- [ ] **Step 13: Run the complete local verification gate**

  Run:

  ```bash
  ruff check autograder/ scripts/ tests/
  mypy autograder/ scripts/
  python -m pytest tests/ -q
  ```

  Exercise the minimum dependency floor in a clean temporary virtual
  environment with `uv pip install --resolution lowest-direct -e .`, install
  pytest there, and run the offline suite.

- [ ] **Step 14: Run the conditional live smoke test**

  If `OPENROUTER_API_KEY` is available, run one minimal synthetic `inspect`
  command through `openrouter/auto-beta`, then repeat it from cache with the key
  unset. If no key is available, record the smoke test as not run; do not weaken
  the offline verification gate.

- [ ] **Step 15: Commit verification fixes, if any**

  If verification required changes, commit them as
  `fix: address OpenRouter integration verification`; otherwise create no empty
  commit.
