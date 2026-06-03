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
- Core built-in tools: filesystem (read/write/list), shell, ask
- Sandbox and permission guards
- Append-only event persistence
- Session lifecycle (start, run turns, close)

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

## Events

All significant state changes are recorded as append-only events:
- `turn_started`, `turn_finished` — turn boundaries
- `session_closed` — session termination
- `error`, `interrupted` — error states
- `mailbox_send`, `mailbox_acknowledge` — inter-agent messages
- `hook_event` — hook-emitted events
