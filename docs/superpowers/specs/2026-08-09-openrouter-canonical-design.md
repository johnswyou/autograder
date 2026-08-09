# OpenRouter Canonical LLM Design

## Objective

Make OpenRouter the repository's only LLM transport. Operators select any
OpenRouter model slug with `--model`; the default is the dynamic
`openrouter/auto-beta` router. Remove the Anthropic SDK, Anthropic request
shapes, Claude-specific validation, cache markers, environment variables, and
documentation.

## Architecture

The grading stages continue to call the deep `run_agent(AgentTask)` interface.
`autograder.llm` owns a narrow internal chat seam:

```text
grading stages -> run_agent -> ChatClient.complete -> OpenRouter Chat Completions
```

`ChatRequest` contains canonical Chat Completions messages, function tools,
model, completion limit, reasoning preference, routing policy, and a session
identifier. `ChatTurn` contains the replayable assistant message, parsed tool
calls, finish reason, refusal/error information, normalized usage, resolved
model, and provider. `OpenRouterChatClient` is the production implementation;
offline tests use a scripted implementation of the same protocol.

This seam is intentionally local to `autograder.llm`. There is one production
transport and one test double, so a provider plug-in registry would add surface
area without simplifying callers.

## Canonical OpenRouter behavior

- Depend on `openrouter>=1.0.3` and Python 3.10 or later.
- Use the stable `/chat/completions` API through `OpenRouter.chat.send` with
  streaming enabled.
- Default to `openrouter/auto-beta`; fixed OpenRouter model slugs remain the
  documented choice for reproducible or high-stakes runs.
- Read credentials from `OPENROUTER_API_KEY` or `--api-key`; do not fall back to
  `ANTHROPIC_API_KEY`.
- Expose optional reasoning effort as
  `none|minimal|low|medium|high|xhigh|max`. Omission uses the selected model's
  default. Remove Claude-family compatibility tables.
- Reuse one nonempty `session_id` for every request in an individual agent
  loop, preserving OpenRouter's sticky model/provider routing.
- Default provider policy to fallbacks enabled, parameter support required,
  zero data retention required, and data collection denied. The CLI offers
  explicit `--allow-data-retention` and `--allow-data-collection` opt-outs.
- Request OpenRouter routing metadata and send static application attribution.
- Rely on OpenRouter/provider automatic prompt caching. Do not emit Anthropic
  `cache_control` markers or retain a prompt-caching switch.

## Messages and tools

System instructions are a `role: system` message. Images are `image_url`
content parts containing a base64 JPEG data URL. Local tools and
`submit_result` use Chat Completions function schemas:

```json
{"type":"function","function":{"name":"submit_result","description":"...","parameters":{}}}
```

The stream assembler joins text and fragmented function arguments by tool-call
index. It constructs one exact assistant message containing `tool_calls`,
`reasoning`, `reasoning_details`, and refusal data when present. That assistant
message is replayed unchanged before one `role: tool` response per tool call.

Malformed JSON arguments and local tool failures become `ERROR:` tool response
text so the model can repair them. Pydantic validation failures for
`submit_result` follow the same repair path. A valid submission completes the
agent without another model call.

## Errors, usage, and auditability

Pre-stream SDK errors, structured mid-stream errors, `length`,
`content_filter`, and exhausted-turn outcomes become contextual `AgentError`
messages. Ordinary `stop` without `submit_result` retains the existing two
nudge attempts.

Usage is normalized as prompt, completion, reasoning, cached-prompt, and
cache-write tokens plus OpenRouter-reported cost. The manifest distinguishes
the requested model from all resolved models and providers observed during the
run. Credentials, prompts, and student content are never added to the
manifest.

The OpenRouter client remains lazy so a cached-only run works without an API
key. `Pipeline.close()` closes both the SDK client and assignment document and
remains idempotent.

## Persistence and migration

Run bindings move from schema 2 to schema 3. Their configuration identity
removes Anthropic thinking/cache settings and adds reasoning plus privacy
routing settings. Existing schema-2 output directories are rejected with the
existing fresh-directory guidance; Anthropic-created artifacts are never
silently mixed with OpenRouter-created artifacts.

## Verification

All model tests remain offline. They cover fragmented tool arguments, multiple
tool calls, malformed arguments, schema repair, exact reasoning-detail replay,
session stickiness, finish/error handling, routing/privacy parameters, usage,
client lifecycle, and the declared SDK floor. The final gate is Ruff, mypy,
the complete pytest suite, the repository CI matrix, and a minimal synthetic
live smoke test when `OPENROUTER_API_KEY` is available.
