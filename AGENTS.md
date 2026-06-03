# XBot Hermes — Agent Guide

This document is written for AI agents (Codex, Claude, or any coding assistant) working on this codebase. It describes project identity, architecture, conventions, and the current state of every subsystem so an agent can make informed decisions without re-discovering the codebase.

## Project Identity

XBot Hermes is an experimental **single-user local agent runtime** built around the principle:

> **State is the system center. The LLM is just one planner/executor/verifier among many.**

The runtime follows these hard rules:
- **File-backed state** — every session gets one DAG state directory; JSONL is append-only truth, YAML is materialized view.
- **Explicit contracts** — `Run`, `Turn`, `Interrupt`, `ContextFrame`, `ToolResultRef` are Pydantic models used at the boundary.
- **Permission-first tools** — every tool call passes through allow/deny/ask rules; sandbox (bubblewrap) is a separate second layer.
- **Append-only event logs** — `events.jsonl` / `graph.jsonl` / `context_tree.jsonl` / `mailbox.jsonl` are never mutated; errors are recorded, not deleted.
- **Plan as executable DAG** — `plan.yaml` is validated, materialized, and versioned, not a markdown wishlist.

## Repository Layout

```
./
├── main.py                       # Legacy terminal entry point; target server launcher
├── pyproject.toml                # uv project metadata
├── README.md                     # User-facing overview
├── AGENTS.md                     # ← This file
├── CLAUDE.md                     # Claude Code pointer → AGENTS.md
├── plan.md                       # Refactoring plan (may be stale)
├── status.md                     # Implementation progress log
├── task.md                       # Long-term design vision
├── xbot/
│   ├── models.py                 # All Pydantic models (config + runtime contracts)
│   ├── config.py                 # YAML/JSON config loading, RuntimePaths
│   ├── context.py                # System prompt assembly, ContextFrame construction
│   ├── graph.py                  # LangGraph state graph (3-node ReAct loop)
│   ├── interaction.py            # HermesInteraction — main runtime entry point
│   ├── runtime.py                # RuntimeContext (session/personality/thread/task identity)
│   ├── compaction.py             # Context compression (prepare_context node)
│   ├── tool_runtime.py           # Tool guardrails, sandbox gate, interrupt handling
│   ├── registry.py               # ToolRegistry and sandbox metadata
│   ├── builtin_tools/            # Canonical built-in tool package
│   ├── tools.py                  # Compatibility re-export only
│   ├── planning.py               # DAG validation, scheduler, plan materialization
│   ├── state.py                  # TaskStateStore — file-backed agent DAG state (1300+ lines)
│   ├── verification.py           # verify_task_state — 11 consistency checks
│   ├── permissions.py            # PermissionSystem (deny > allow > ask > default)
│   ├── sandbox.py                # Bubblewrap sandbox policy and execution
│   ├── skills.py                 # Skill loading (read-only markdown)
│   ├── checkpoint.py             # FileBackedSaver (LangGraph checkpoint → pickle)
│   ├── cache.py                  # ToolResultCache (file-backed, session-scoped)
│   ├── llm.py                    # LLM factory (openai/anthropic/smoke providers)
│   ├── smoke_llm.py              # SmokeRefactorLLM — deterministic test double
│   ├── mock_llm.py               # MockLLM — sequence-based test double
│   └── terminal.py               # TerminalSession — CLI adapter
├── data/
│   ├── config/                   # provider.yaml, user.yaml, system_template.md
│   ├── personalities/<id>/       # personality.yaml, instructions.md, memory.md, permissions.json, sandbox.json, skills/
│   └── sessions/<id>/            # workspace/, state/, saver/, cache/, subagents/
├── docs/                         # User-facing documentation
├── tests/                        # Test suite
├── scripts/                      # provider_smoke_refactor.py
└── .claude/                      # Claude Code settings
```

## Architecture: The Loop

The current LangGraph loop is intentionally minimal — exactly 3 nodes:

```
START → prepare_context → agent → tools → (back to prepare_context)
                                ↓
                              END (when no tool calls requested)
```

- **prepare_context** (`compaction.py`): Compresses old messages into a summary node when context exceeds thresholds. Also runs on the first turn for stale-event cleanup.
- **agent** (`graph.py:make_agent_node`): Assembles a `ContextFrame` (system prompt + runtime state + sanitized message chain), binds tools, calls the LLM.
- **tools** (`tool_runtime.py:make_tool_node`): Three-layer processing: sandbox gate → permission guardrail → execution → output guardrail (large results cached). Permission/sandbox `ask` triggers a combined `tool_confirm` interrupt.

The loop does **not** enforce Plan/Act/Observe/Verify stages — that concept lives at the **task mode** level, driven by the LLM calling plan tools.

## Architecture: State System

`TaskStateStore` (`state.py`) is the most critical module. Every session gets one primary DAG state at:

```
data/sessions/<session_id>/state/
  task.yaml          # Metadata (mode: chat|task, schema_version, timestamps)
  goal.md            # Task goal (set by task_begin)
  plan.yaml          # Executable DAG (validated, versioned)
  events.jsonl       # Runtime events (turn_start, turn_finish, interaction events)
  graph.jsonl        # Execution trace (tool calls, interrupts, artifacts)
  context_tree.jsonl # Context tree nodes (turn, message, tool, error)
  mailbox.jsonl      # Inter-agent messages (send, read, ack)
  state.yaml         # Materialized view (from events + graph + context_tree + mailbox)
  context.md         # Context projection for the model
  claims.yaml        # Structured claims with evidence
  artifacts/         # Produced files
  checkpoints/       # Legacy checkpoint dir (versions/ is the active one)
  versions/plans/    # Plan version snapshots (index.yaml + snapshot files)
  summaries/         # Compaction and manual summary markdown files
  locks/             # Lock files (created but not used)
```

Key invariants:
- `events.jsonl`, `graph.jsonl`, `context_tree.jsonl`, `mailbox.jsonl` are **append-only**. Never delete or modify lines.
- `state.yaml` is **materialized** from those logs — it's a cache, not source of truth.
- `plan.yaml` versioning: every mutation snapshots the before/after plan to `versions/plans/`.

## Architecture: Identity Model

```
session_id  → namespaces workspace/cache/state/saver/subagents
personality_id → selects instructions/memory/permissions/sandbox config
thread_id   → LangGraph checkpoint thread key (not a state directory)
task_id     → DAG state subject; "agent" for main agent, subagent_id for children
```

Do **not** create nested `task/default/` directories. The primary agent uses `state/` directly. Subagents use `subagents/<id>/state/`.

## Built-in Tools (35 total)

Tools are defined in `xbot/builtin_tools/` and loaded through `ToolRegistry`. `xbot/tools.py` is a compatibility re-export only. They are grouped by function:

| Group | Tools |
|-------|-------|
| Filesystem | `shell`, `filesystem_read`, `filesystem_write`, `filesystem_list` |
| Communication | `ask`, `message_send` |
| Task mode | `task_begin`, `task_status`, `task_exit` |
| Plan DAG | `plan_add_nodes`, `plan_autofill`, `plan_next`, `plan_update`, `plan_node_history`, `plan_remove_node` |
| Summary | `summary_add`, `summary_list`, `summary_read` |
| Claims | `claim_add`, `claim_list` |
| Subagent | `subagent_create`, `subagent_wait`, `subagent_list`, `subagent_stop` |
| Memory | `memory_update`, `memory_list`, `memory_search` |
| Context | `context_head`, `context_rewind` |
| Mailbox | `mailbox_send`, `mailbox_read` |
| Debug | `debug_analyze` |
| Cache | `cache_read` |
| Compaction | `compact` |
| Skill | `skill_load` |

Tools must be exported from `xbot.builtin_tools` and registered in canonical `TOOL_SANDBOX_MODE` metadata for sandbox to recognize them. Unknown tools are rejected when sandbox is enabled.

## Task Mode and DAG

Task mode is the structured execution mode. The flow:

1. `task_begin(goal, steps_json)` — records goal, replaces DAG, writes `goal.md` and `context.md`
2. `plan_autofill(scope)` — generates standard inspect/implement/verify/report DAG skeleton
3. `plan_next()` — scheduler picks the highest-priority ready node and marks it running
4. `plan_update(node_id, status)` — advances node to completed/verified/failed/blocked
5. `task_exit(status="completed")` — exits task mode; rejects if DAG has unfinished nodes

Scheduler priority order (`planning.py:_priority_key`):
1. Verification nodes first (type_priority=0)
2. Explicit `priority` field (ascending, default 100)
3. Dependency count (ascending)
4. Node ID (alphabetical tiebreaker)

DAG events are attributed to the active `plan_node_id`. `state.yaml.dag` exposes per-node activity counts.

## Key Design Constraints

When modifying this codebase, respect these constraints:

1. **Never mutate append-only files.** Events are immutable. Corrections are new events.
2. **Materialized state is derived.** `state.yaml` must be reproducible from the JSONL files alone.
3. **Plan changes are versioned.** Don't overwrite `plan.yaml` without snapshotting the old version.
4. **Large outputs stay out of context.** Use `ToolResultCache` + `cache://` refs, not inline payloads.
5. **Sandbox is fail-closed.** If bubblewrap is unavailable, sandboxed tools fail — never fall back to host execution.
6. **Permission deny wins.** The matching order is deny → allow → ask → default.
7. **Thread_id is just a checkpoint key.** Don't use it to derive state paths.
8. **Session state at `state/`, not `tasks/default/`.** The primary agent has one DAG at the session root.
9. **Subagents use `subagents/<id>/state/`.** They don't create sibling task directories.
10. **Trace events are off by default.** Set `XBOT_TRACE_EVENTS=1` to persist detailed interaction traces.

## Testing Conventions

### Test files

| File | Purpose | Quality |
|------|---------|---------|
| `tests/test_runtime_boundaries.py` | Primary suite — state, sandbox, streaming, DAG, context tree, mailbox, subagent (43 tests) | ✅ Good |
| `tests/test_agent.py` | Legacy suite — mock-based, shallow assertions, doesn't test file state (19 tests) | ⚠️ Needs rewrite |
| `tests/test_personality_runtime.py` | Config layout + smoke provider refactor (2 tests) | ⚠️ Smoke uses mock |

### Running tests

```bash
uv run pytest -q                                    # All tests
uv run pytest -q -k "task_state"                    # Specific filter
python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py xbot/builtin_tools/*.py xbot/hooks/*.py tests/*.py
```

### Writing new tests

- Use `temp_data_dir` fixture for file system tests — never touch real `data/sessions/default`.
- Every graph test must use a unique `thread_id`.
- New tools must be exported from `xbot.builtin_tools`, added to canonical `TOOL_SANDBOX_MODE`, and have registry/sandbox registration tests.
- Test through `HermesInteraction` for user-visible events; test through `build_agent_graph` for message state.
- For streaming, assert `InteractionEvent` objects, not raw provider chunks.
- Compaction tests need at least two turns — compression happens before the next model call.

### Smoke test

```bash
uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke
```

Requires real provider credentials. Validates: tool execution trail, DAG attribution, file content changes, state file integrity, claim/summary presence.

## Known Issues and Gaps

### Architecture debt (current)

| Issue | Severity | Detail |
|-------|----------|--------|
| No C/S protocol boundary | 🔴 Critical | `TerminalSession` still runs in-process and renders LangChain message/chunk/tool objects. The next path is runtime server + JSONL protocol client. |
| InteractionEvent is not a wire contract | 🔴 Critical | Internal `InteractionEvent.payload` is `Any`; protocol frames still need version, seq, request_id, turn_id, tool_call_id, and stable payload schemas. |
| Tool lifecycle events incomplete | 🔴 Critical | shell/exec UI cannot reliably distinguish pending, approved, running, completed, failed, interrupted, sandbox denied, or permission denied. |
| No schema migration | 🔴 Critical | `STATE_SCHEMA_VERSION=1` is written but never checked. YAML files have no upgrade path. Old task directories will silently fail on schema changes. |
| Loop stages not separable | 🔴 Critical | Plan/Act/Observe/Verify exist only as LLM-called tools, not as loop-enforced stages. No executor interface for swapping strategies. |
| Context tree not in prompt | 🟡 Medium | `context_tree.jsonl` exists and is queryable via tools, but `project_context()` doesn't include tree nodes. The model can't "see" the tree without calling tools. |
| DAG scheduler incomplete | 🟡 Medium | Missing: unblock-chain priority, low-cost diagnosis priority, risk-level gating, parallel worker dispatch. |
| Multi-agent paused | 🟡 Medium | Attach/mailbox/detach MVP exists, but async scheduler and multi-agent UI are intentionally not being advanced now. |

### Test gaps

- No event replay → state reconstruction test
- Protocol golden tests are not implemented yet
- No runtime server stdio JSONL tests yet
- No protocol renderer tests for shell/exec lifecycle yet
- No complex DAG topology tests (diamond, 5+ chains, partial multi-deps)
- No subagent sandbox isolation test
- No compaction semantic correctness test beyond source refs/group safety
- `test_agent.py` uses wrong config path (`"personality"` singular, should be `"personalities"`)

### Code quality

- Silent or broad exception handling remains in terminal/test compatibility paths; check current line numbers before editing.
- 8 unused test helper functions in test_agent.py (lines 65-97)
- `RuntimePaths.tasks_dir` and `checkpoints_dir` defined but never used
- `graph.py` imports `AIMessage`, `ToolMessage`, `split_for_compaction` but doesn't use them directly
- `verification.py` checks only 11 items; `task.md` specifies ~21 required checks

## Development Workflow

1. **Read `status.md`** to understand current progress and what's been completed.
2. **Read `plan.md`** for the refactoring strategy (may be stale — verify against code).
3. **Read `task.md`** for the long-term design vision.
4. **Run the test suite** before making changes: `uv run pytest -q`
5. **Run compile check** after changes: `python -m py_compile main.py xbot/*.py tests/*.py`
6. **Commit stable nodes** — commit when tests pass and smoke works, not mid-debugging.
7. **Update `status.md`** after completing a meaningful increment.

## Environment

- Python 3.10+
- Package manager: `uv`
- Test runner: `pytest` with `asyncio_mode = "auto"`
- Provider: DeepSeek (OpenAI-compatible), config in `data/config/provider.yaml`
- Sandbox: bubblewrap (auto-detected; tests skip if unavailable)
