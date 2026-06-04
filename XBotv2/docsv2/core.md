# Core Engine

## ReAct Loop

The core engine implements a minimal 3-node ReAct loop:

```
prepare_context → agent → tools → repeat
                         ↘ (no tool calls) → END
```

At each stage, registered hooks run before and after the core logic.
Loop hooks (before/after context/agent/tools) can short-circuit the
stage by returning a truthy value.

## Without Plugins

The engine works without any plugins. It provides:
- Linear ReAct loop with context → LLM → tool execution
- Core built-in tools: filesystem (read/write/list), shell, and interaction
  tools
- Sandbox and permission guards
- Default tool-result caching for oversized outputs
- Append-only event persistence
- Session lifecycle (start, run turns, close)

LLM providers are selected explicitly. Unknown provider names raise a
configuration error instead of silently falling back to another protocol.
Bootstrap validates runtime identifiers (`personality_id`, `provider_name`,
`session_id`, `thread_id`) with a conservative whitelist of letters, numbers,
dot, underscore, and dash before any session paths are created.

Permission rules still support the tri-state `allow`/`deny`/`ask` model, but
the `ask` decision currently emits a `permission_request` event and fails
closed during tool execution because the JSONL/TUI interrupt-resume approval
flow is not implemented yet. Denials emit `permission_denied`. Both decisions
also pass through dedicated permission hooks and the generic `ON_CLIENT_EVENT`
hook before they are streamed to clients.

The previous placeholder `ask` tool is intentionally not registered in core.
Core now exposes two event-driven interaction tools instead:

- `send_message`: emits a non-blocking `client_message` event and lets the
  current ReAct turn continue.
- `ask_user`: emits `user_input_required`, appends an `interrupted` event, and
  stops the current turn. Resuming from the answer is still a protocol/TUI
  feature gap.

All client-directed interaction events pass through `ON_CLIENT_EVENT` before
persistence and protocol emission, so plugins can audit or meter them without
depending on tool-result internals.

## Built-in Filesystem Tools

Filesystem tools return JSON text instead of unstructured prose.
`filesystem_read` includes content plus path, resolved path, size, mtime,
line count, returned line count, and truncation flags. `filesystem_list`
returns entry metadata and truncation information. `filesystem_write`
supports these modes:

- `overwrite`
- `append`
- `prepend`
- `insert_line`
- `replace_lines`
- `regex_replace`
- `apply_patch` using a single-file unified diff

Sandboxed tool execution resolves path-like arguments to the configured
workspace before invoking the tool, so relative paths are not interpreted
against the process working directory.

## With Plugins

Plugins extend the engine by:
1. **Registering hooks** — inject behavior at any lifecycle stage
2. **Adding tools** — extend the agent's capabilities
3. **Injecting prompt fragments** — add context sections
4. **Owning state** — persistent key-value store per plugin

## Context Building

The context builder assembles provider message lists with injection points:

```
[SystemMessage: system prefix (stable, memoized)]
[SystemMessage: plugin fragments at system_instructions]
[SystemMessage: runtime rules]
[SystemMessage: plugin fragments at system_rules]
[SystemMessage: sandbox summary]
[... message history (sanitized) ...]
[SystemMessage: plugin fragments at dag_suffix]
[SystemMessage: current state]
```

### Fragment Injection Stages

| Stage | Position | Used By |
|-------|----------|---------|
| `system_prefix` | After system base prompt | Rare |
| `system_instructions` | After instructions | Skills, Planning |
| `system_rules` | After runtime rules | Compact |
| `dag_suffix` | Before current state | Planning |

### Cache Design
- Stable prefix memoized per session (keyed by config hash)
- Instance-level cache (no module-level globals)
- Invalidation on fragment registration or config change

## Tool Result Cache

Bootstrap registers a default `AFTER_TOOLS` hook from
`xbotv2.tools.result_cache`. Before tool messages enter history or are emitted
over JSONL, the hook writes oversized outputs to
`state/artifacts/tool_results/` and replaces the inline content with a bounded
summary containing the cache path, original size, preview size, and preview.
This keeps long shell/read outputs from inflating the next context.

## Message Persistence

`messages.jsonl` stores enough LangChain message metadata for deterministic
session resume: message `id`, `name`, AI `tool_calls`, ToolMessage
`tool_call_id`, `status`, `artifact`, provider-facing `additional_kwargs`, and
`response_metadata`. Internal `additional_kwargs` whose keys start with
`xbotv2_` are not restored into message history because those side-channel
events are persisted separately in `events.jsonl`.

`Engine.start_session()` treats either persisted events or persisted messages
as an existing session and runs `ON_SESSION_RESUME`. Only a store with neither
events nor messages runs `ON_SESSION_START`.

## Events

All significant state changes are recorded as append-only events:
- `turn_started`, `turn_finished` — turn boundaries
- `session_closed` — session termination from direct engine close or protocol
  `shutdown`
- `error`, `interrupted` — error states
- `client_message`, `user_input_required` — user interaction events from tools
- `permission_request`, `permission_denied` — permission/sandbox decisions
- `mailbox_send`, `mailbox_acknowledge` — inter-agent messages
- `hook_event` — hook-emitted events
- `tool_result_cached` — large tool output persisted to artifacts and truncated inline

`state.yaml` status is rebuilt from ordered events. A new `turn_started`
reactivates prior `error` or `interrupted` sessions, while `turn_finished`
does not hide an interruption raised during the same turn.
