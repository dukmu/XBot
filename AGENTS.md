# XBot Hermes — Agent Guide

This file is a stable constraint guide for agents working in this repository. It is not a progress log. Current implementation status belongs in `status.md`; refactor strategy belongs in `plan.md`; detailed architecture belongs in `docs/architecture.md`.

## Project Identity

XBot Hermes is a single-user local agent runtime built around one principle:

> State is the system center. The LLM is one planner/executor/verifier/summarizer among many.

Hard rules:

- File-backed state is the durable truth.
- JSONL event logs are append-only.
- `state.yaml` and `context.md` are materialized views, not source of truth.
- Tool calls pass permission checks and sandbox policy before execution.
- Large tool outputs use cache refs instead of entering prompt context directly.
- Complex work uses executable DAG task mode, not a markdown todo list.
- Never Keep Legacy code for compactiability

## Repository Map

```text
./
├── main.py                 # CLI launcher; server mode should stay thin
├── plan.md                 # Current refactor plan
├── status.md               # Current progress log
├── docs/                   # User/developer documentation
├── tests/                  # Test suite
├── scripts/                # Smoke and utility scripts
├── xbot/
│   ├── interaction.py      # HermesInteraction runtime boundary
│   ├── protocol.py         # C/S protocol schema and event encoding
│   ├── server.py           # Runtime server transport
│   ├── terminal.py         # Terminal client/renderer
│   ├── runtime.py          # RuntimeContext and RuntimeFrame
│   ├── context.py          # ContextProjection -> provider messages
│   ├── compaction.py       # Auditable context compaction
│   ├── graph.py            # LangGraph executor wiring
│   ├── tool_runtime.py     # Tool guard/execution/cache orchestration
│   ├── registry.py         # ToolRegistry and sandbox metadata
│   ├── builtin_tools/      # Canonical built-in tools
│   ├── state.py            # TaskStateStore append-only state
│   ├── checkpoint.py       # FileBackedSaver checkpoint
│   ├── sandbox.py          # Bubblewrap sandbox policy/execution
│   └── permissions.py      # Permission rules
└── data/
    ├── config/
    ├── personalities/<id>/
    └── sessions/<id>/
```

## Identity Model

Do not mix these identifiers:

```text
session_id      workspace/cache/state/saver/subagents namespace
personality_id  instructions/memory/permissions/sandbox/skills selection
thread_id       LangGraph checkpoint conversation key
task_id         DAG state subject; main agent is "agent"
```

Path rules:

- Main agent state lives at `data/sessions/<session_id>/state/`.
- LangGraph checkpoint lives under `data/sessions/<session_id>/saver/`.
- Tool cache lives under `data/sessions/<session_id>/cache/`.
- Subagents live under `data/sessions/<session_id>/subagents/<subagent_id>/`.
- Do not create `tasks/default/` or sibling sessions for subagents.

## State Rules

Append-only files:

```text
events.jsonl
graph.jsonl
context_tree.jsonl
mailbox.jsonl
```

Never edit or delete prior JSONL rows. Corrections are new events.

Materialized/projection files:

```text
state.yaml
context.md
claims.yaml
summaries/*.md
versions/plans/*
```

`state.yaml` must be reproducible from append-only logs and current plan files.

## Runtime And UI Boundary

The runtime owns model execution, tools, state, permissions, sandbox, checkpoints, and interrupts. UI clients must not parse provider or LangGraph internals.

Required C/S direction:

```text
client/TUI command
  -> JSONL protocol frame
  -> runtime server
  -> HermesInteraction
  -> internal InteractionEvent
  -> protocol event
  -> client/TUI renderer
```

UI constraints:

- Do not import LangChain or LangGraph in UI rendering code.
- Do not parse `AIMessage`, `AIMessageChunk`, or `ToolMessage` in the renderer.
- Render tool calls from protocol `tool.*` events only.
- A tool lifecycle is keyed by `tool_call_id`.
- Interrupt/resume is keyed by `interrupt_id` and `request_id`.
- Large stdout/stderr payloads use `cache://` refs.

## Context Rules

Context is built explicitly:

```text
RuntimeFrame -> ContextProjection -> provider messages
```

`context.py` should not discover config, state, or runtime globals on its own. The runtime prepares all inputs.

Message layout should stay cache-friendly:

```text
[stable system prefix]
[history messages]
[dynamic task suffix]
```

Compaction rules:

- Keep AI tool-call messages and matching tool results together.
- Do not compact unresolved tool calls or interrupts.
- Write summary artifacts with source refs/ranges.
- Record compaction in graph/context-tree state.

## Tools, Permissions, And Sandbox

Canonical tools live in `xbot/builtin_tools/` and are loaded through `ToolRegistry`. Do not add compatibility tool modules or fallback import paths.

Every enabled tool must have sandbox metadata:

```text
host | sandboxed
```

Permission order is:

```text
deny -> allow -> ask -> default
```

Sandbox rules:

- Sandbox is a second layer after permissions.
- Bubblewrap sandbox is fail-closed.
- Unknown sandboxed tools are rejected.
- Do not fall back to host execution when sandbox is required.
- Denied/ask paths must be enforced at the sandbox/resource layer, not only by Python path checks.

## Task Mode

`plan.yaml` is executable DAG state. Complex work should use:

```text
task_begin
plan_autofill / plan_add_nodes / plan_remove_node
plan_next
plan_update
task_status
task_exit
```

Rules:

- One running DAG node at a time unless an explicit scheduler change is implemented.
- Plan mutations are versioned.
- `completed` and `verified` both satisfy dependencies.
- `task_exit(status="completed")` must reject unfinished, blocked, or failed nodes.

## Testing

Default verification:

```bash
uv run pytest -q
python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py xbot/builtin_tools/*.py xbot/hooks/*.py tests/*.py
```

Provider smoke, when credentials are available:

```bash
uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke
```

Testing rules:

- Use `temp_data_dir`; do not write real `data/sessions/default` in tests.
- Every graph test gets a unique `thread_id`.
- New tools need registry and sandbox metadata tests.
- Runtime-visible behavior should be tested through `HermesInteraction` or protocol/server tests.
- UI behavior should be tested through protocol events, not LangChain objects.

## Documentation Discipline

- Keep `AGENTS.md` stable and constraint-oriented.
- Put progress updates in `status.md`.
- Put implementation plans in `plan.md`.
- Put detailed architecture explanations in `docs/architecture.md`.
- Do not turn this file into a changelog or per-branch audit.
