# XBotv2 Core Runtime

## Engine (`xbotv2/core/engine.py`)

ReAct loop: user message → context → LLM → tools → repeat.
Uses XBot-owned `Message` dataclass exclusively. No LangChain dependency.

`_run_turn_impl()` coordinates explicit stage methods for message admission,
context construction, model-request preparation, streamed response handling,
tool batches, and turn finish. Stage-specific methods retain their own Hook
return rules; internal completion records are not protocol events.

### Streaming

Provider `stream=True` yields per-token `ModelChunk` objects.
Engine emits `assistant_message_delta` events for each content delta and
`tool_call_delta` for partial tool calls. Final response aggregated into
`ModelResponse` and emitted as an `assistant_message` event.

Timer-based TUI rendering (`_stream_timer` at 50ms intervals) ensures
per-token overhead is near-zero.

### Reasoning / Thinking

Provider extracts `reasoning_content` from streaming deltas (DeepSeek thinking mode).
Emitted as regular `assistant_message_delta` with `## Thinking` header.
Stored in `Message.additional_kwargs.reasoning_content`.
Re-passed to API for tool-call turns via `provider_messages`.

### Hooks

41 `HookStage` values cover the existing lifecycle. Key stages:
`BEFORE_USER_MESSAGE_ACCEPT`, `AFTER_CONTEXT`, `BEFORE_MODEL_REQUEST`,
`AFTER_AGENT`, `BEFORE_TOOLS`, `ON_STOP`, `ON_STOP_FAILURE`,
`ON_TOOL_CALL_FAILURE`, `PRE_COMPACT`, `POST_COMPACT`, `BEFORE_TOOL_CALL`,
`ON_PERMISSION_REQUEST`, `ON_SESSION_INIT`.

Guard hooks return explicit `HookDecision` values. Transform hooks return a
stage-specific dictionary. Observer hooks run all callbacks and ignore results.
See [hooks.md](hooks.md).

ExceptionGroup from strict hooks (ON_SESSION_INIT, ON_SESSION_CLOSE,
BEFORE_STATE_PERSIST, AFTER_STATE_PERSIST, ON_STOP) caught with BaseException.

### Compaction

`_handle_compaction()` method: BEFORE_CONTEXT short-circuit → PRE_COMPACT
hook → message replacement → POST_COMPACT hook. Depth-4 nesting extracted
to dedicated method.

## Tools

### Tool (`api/tools.py`)

```python
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    function: Callable
    parameters: dict          # JSON Schema
```

`from_function()` extracts docstrings and signatures. Supports async functions
via `ainvoke()`. Keyword-only parameters with defaults (like `sandbox=None`)
are injected at invocation time.

### ToolRegistry (`tools/registry.py`)

Identity is the canonical registered name. Built-in core keys are bare (for
example `shell`); non-core examples include `plugin:skills:skill`,
`skills:global:find-skills`, and `mcp:github:search`.

`restrict()` supports canonical keys, namespace selectors such as
`skills:*` and `mcp:*`, and bare display-name fallbacks.

`get()` matches by both registry key and display name (fallback).

### Sandbox (`tools/sandbox.py`, `tools/sandbox_bwrap.py`)

`BubblewrapBackend` provides process isolation via `bwrap`.
`SandboxPolicy` exposes capability methods: `run_shell`, `read_file`,
`write_file`, `list_dir`. Tools call these directly via `sandbox` kwarg
injection. Bwrap mounts enforce access control at OS level — no Python
path extraction/checking.

### Permissions (`tools/permissions.py`)

Tri-state: deny → allow → ask → default. Regex pattern matching on
tool names and parameters. `BEFORE_TOOL_CALL` may reject or transform a call;
the permission system then checks the final tool name and arguments. Continuing
from the hook does not bypass core permission policy.

## Persistence

```
data/sessions/<sid>/state/
├── messages.jsonl          # append-only messages and history operations
├── plugin_states/          # per-plugin YAML files
└── artifacts/              # cached large tool outputs and provider context
```

`CoreStateStore` (`persistence/store.py`):
- `sync_messages()`: append normal message extensions
- `append_checkpoint()`: append a Compact or explicit replacement baseline
- `append_undo()` / `append_clear()`: append replayable stack operations
- `append_mailbox_delivery()`: record delivered Mailbox input without requeueing it
- `read_messages()`: replay the latest checkpoint, later messages, Undo, and Clear
- `has_existing_session()`: session resume detection
- `_max_msg_id` cached to avoid O(n) scan

Old message-only files remain readable. No operation removes earlier JSONL
records; Compact replay starts at the last checkpoint for bounded reconstruction.
There is no separate `events.jsonl` or `state.yaml`.

`SessionRuntime` (`core/session.py`) owns live-only turn, Mailbox, interaction,
and event-stream state. It is shared by HTTP and once mode, while resume still
reconstructs only persisted Engine and plugin state. Mailbox queues and pending
interaction waiters are never replayed. Started deliveries remain journal audit
records separate from reconstructed provider Messages.

## Context Builder (`core/context.py`)

Assembly order:
```
[system_prefix]
[plugin fragments: system_instructions stage]
[runtime rules]
[sandbox summary]
[message history]
[plugin fragments: context_suffix stage, if any]
[active subagent count, if non-zero]
```

This is component render order. Final model messages contain one leading system
message with all system components, followed by non-system conversation history.
No provider receives a trailing system message from `context_suffix`.
The default prompt contains no clock or turn counter, so repeated model calls
retain a stable prefix. Skill activation uses its normal Tool-result or
prompt-expansion message and is not repeated as a system marker.

Cache key uses tuple (was SHA256). `_sanitize_history` removes orphaned
tool messages before provider conversion.

## LLM Provider (`llm/client.py`)

`OpenAICompatibleProvider` (OpenAI, DeepSeek, LM Studio) and `AnthropicProvider`.
Shared configuration via `reasoning_effort` and `thinking_enabled`.
`provider_values()` extracts all config from dict or Pydantic model via `_get_cfg`.

`provider_messages()` converts XBot `Message` → provider format.
Preserves `reasoning_content` for tool-call turns.

## Startup (`core/bootstrap.py`)

Order: config → state store → hooks → tools → sandbox → permissions →
plugins → LLM → ON_SESSION_INIT → restrict → engine.

`restrict()` runs AFTER `ON_SESSION_INIT` so plugin-discovered tools
are included in the enabled set.
