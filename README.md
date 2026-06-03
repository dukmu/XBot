# XBot Hermes

Hermes is a lightweight, single-user local agent built around a state-centered runtime: explicit context construction, permission interrupts, active ask, auditable context compression, file-backed agent DAG state, hooks, a pluggable tool registry, and protocol-ready UI boundaries.

The current `claude-refactor` branch has a working runtime path with file-backed state, context tree, mailbox, attach/detach subagent MVPs, auditable compaction, claims/summaries, real provider smoke coverage, and a JSONL client/server protocol boundary for terminal/TUI clients.

## Design Intent

Hermes is meant to be a personal agent, not a multi-tenant service. The main design goals are:

- **Single-user local runtime**: one user, one local workspace, configurable local sessions.
- **Explicit model context**: `RuntimeFrame -> ContextProjection -> provider messages`.
- **Permission-first tools**: tool calls go through allow/deny/ask rules; sensitive actions interrupt for confirmation.
- **System sandbox**: when enabled, host-touching tools run inside bubblewrap with explicit resource mounts and deny/ask masking.
- **Active ask**: the agent can pause and ask the user for missing intent or decisions.
- **File-backed agent state**: each session gets one primary DAG state directory with append-only runtime and graph logs plus materialized YAML state.
- **Context compression**: when context grows too large, older history becomes a compacted summary node.
- **Context tree**: message/tool/summary/rewind nodes are append-only and inspectable.
- **Protocol UI boundary**: terminal/TUI clients should consume JSONL protocol events, not LangChain message objects.
- **Tool cache hooks**: large tool results are cached and referenced instead of copied wholesale into context.

See [docs/architecture.md](./docs/architecture.md) for the full Hermes architecture.

For AI agents and developers working on this codebase, read [AGENTS.md](./AGENTS.md) for architecture, conventions, known issues, and development workflow. [CLAUDE.md](./CLAUDE.md) provides a quick-reference entry point for Claude Code.

## Current Capability Status

| Capability | Status |
|------------|--------|
| LangGraph ReAct loop | Implemented |
| Terminal adapter | Protocol client |
| Curses TUI | Live protocol MVP |
| Runtime server protocol | Implemented MVP |
| Provider config | Implemented |
| Permission allow/deny/ask | Implemented |
| Permission interrupt confirmation | Basic implementation |
| System sandbox | Implemented MVP |
| Active ask | Basic implementation |
| Context compression | Basic linear implementation |
| Tool result cache hooks | File-backed with metadata |
| Plan/DAG state | Implemented MVP |
| Context tree and rewind | Implemented MVP |
| Subagents (attach/detach MVP) | Implemented MVP; expansion paused |
| Mailbox | Implemented MVP |
| File-backed agent state | Implemented MVP |
| Hooked loop architecture | Implemented (hooks.py + registry.py) |
| Pluggable tool registry | Implemented (ToolRegistry) |
| Cache-friendly DAG context | Implemented (context.py) |
| SQLite persistence | Planned as optional index |
| Runtime persistence default | `FileBackedSaver` / `InMemoryStore` |

## Repository Layout

```text
./
├── main.py                    # Terminal client launcher and server subcommand
├── pyproject.toml             # Project metadata and uv dependencies
├── README.md
├── AGENTS.md                  # Architecture guide for AI agents
├── CLAUDE.md                  # Quick reference for Claude Code
├── xbot/
│   ├── models.py              # Pydantic models and state types
│   ├── config.py              # Configuration loading
│   ├── compaction.py           # Context compaction phase
│   ├── context.py             # Context-frame construction
│   ├── permissions.py         # Permission system
│   ├── registry.py            # ToolRegistry and sandbox metadata
│   ├── builtin_tools/         # Canonical built-in tools
│   ├── tool_runtime.py         # Tool guardrails, interrupts, sandbox execution hooks
│   ├── planning.py            # Executable plan DAG validation and scheduling helpers
│   ├── skills.py              # Skill discovery and loading
│   ├── state.py               # File-backed agent DAG state and event materialization
│   ├── llm.py                 # LLM factory
│   ├── graph.py               # LangGraph state graph
│   ├── interaction.py         # P0 interaction runtime and normalized events
│   ├── runtime.py             # Explicit runtime context
│   ├── verification.py        # File-backed task-state verification helpers
│   ├── protocol.py            # JSONL C/S protocol schema and encoder
│   ├── server.py              # JSONL runtime server
│   ├── terminal.py            # Protocol terminal client/renderer
│   ├── tui.py                 # Protocol-driven curses TUI state/client
│   └── mock_llm.py            # Test model
├── docs/
│   ├── README.md
│   ├── architecture.md
│   ├── configuration.md
│   ├── getting-started.md
│   └── testing.md
├── tests/
│   └── test_agent.py
└── data/
    ├── config/
    │   ├── provider.yaml
    │   ├── system_template.md
    │   └── user.yaml
    ├── personalities/
    │   └── default/
    │       ├── personality.yaml
    │       ├── instructions.md
    │       ├── memory.md
    │       ├── permissions.json
    │       ├── sandbox.json
    │       └── skills/
    ├── sessions/
    │   └── default/
    │       ├── workspace/
    │       ├── cache/
    │       ├── saver/
    │       ├── state/
    │       └── subagents/
```

## Installation

Using uv is recommended:

```bash
uv sync
uv sync --all-extras
```

The project does not currently include a `requirements.txt`; use `uv` or install dependencies from `pyproject.toml`.

## Configuration

Configure the provider in `data/config/provider.yaml`:

```yaml
name: "deepseek"
type: "openai"
base_url: "${DEEPSEEK_OPENAI_BASE_URL}"
api_key: "${DEEPSEEK_API_TOKEN}"
model: "deepseek-v4-flash"
max_concurrent: 2
```

Configure the user in `data/config/user.yaml`:

```yaml
user_id: "local_user"
user_name: "Alice"
platform: "local"
session_type: "private"
```

Configure the active personality in `data/personalities/<personality_id>/personality.yaml`:

```yaml
name: "default"
provider: "deepseek"
agent_role: "A local code-focused assistant that makes small, auditable changes."
max_context_tokens: 8000
include_reasoning: false
tools:
  - filesystem
  - ask
  - message_send
skills: []
```

Configure permissions in `data/personalities/<personality_id>/permissions.json`:

```json
{
  "default": "ask",
  "ask_timeout": 60,
  "allow": [
    {"tool": "shell", "params": {"command": "^(ls|cat|pwd|echo)$"}},
    {"tool": "message_send", "params": {}}
  ],
  "deny": [
    {"tool": "shell", "params": {"command": "^(rm|sudo|chmod).*$"}}
  ]
}
```

See [docs/configuration.md](./docs/configuration.md) for the current config model.

## Usage

```bash
python main.py
```

Useful flags:

```bash
python main.py --print-tools
python main.py --print-thoughts
python main.py --no-sandbox
python main.py tui
```

The CLI and TUI are protocol clients. They start a JSONL runtime server subprocess; only the server owns `xbot.interaction.HermesInteraction`. Rendering consumes protocol frames and does not parse LangChain messages. If the active personality has no `sandbox.json`, the runtime enables a conservative bubblewrap sandbox by default. Use `--no-sandbox` only for debugging.

## Runtime Graph

Current graph:

```text
START -> prepare_context -> agent -> tools -> prepare_context
                                \-> END
```

Permission confirmation is handled inside the tools node via LangGraph interrupt/resume.

## File-Backed Agent State

`HermesInteraction.create()` initializes one agent DAG state directory per session at:

```text
data/sessions/<session_id>/state/
```

The directory contains `task.yaml`, `goal.md`, `plan.yaml`, `events.jsonl`, `graph.jsonl`, `state.yaml`, `context.md`, `claims.yaml`, `artifacts/`, `checkpoints/`, `summaries/`, and `locks/`. `events.jsonl` and `graph.jsonl` are append-only logs; `state.yaml` is materialized from those logs so runtime state can be inspected without replaying LangGraph internals.
LangGraph checkpoints are stored separately under `data/sessions/<session_id>/saver/`. Attach-mode subagents use `data/sessions/<session_id>/subagents/<subagent_id>/state/` and their own `saver/`.

Large tool results are cached under `data/sessions/<session_id>/cache/tool-results/` when the runtime is created through `HermesInteraction.create()`. The model receives a `cache://tool-result/<digest>` ref and can read focused slices with `cache_read`.

`plan.yaml` is an executable DAG, not only notes. The runtime validates node dependencies, exposes `ready_nodes` and `active_node` in `state.yaml`, and snapshots plan changes under `versions/plans/`.
Runtime events, graph projections, artifacts, and summaries are attributed to the current running plan node when task mode is active. `state.yaml` exposes per-node DAG activity, and `plan_node_history` can inspect the event history for a specific node.
Plan mutation and scheduling tools require task mode. A task cannot exit with `completed` while the DAG still has unfinished, blocked, or failed nodes; use `cancelled` or `failed` for explicit early exits.
The system prompt also instructs the model to use task mode for complex multi-step work and to drive the DAG through `plan_next` / `plan_update`.

## Built-in Tools

| Tool | Current behavior |
|------|------------------|
| `shell` | Runs inside bubblewrap when sandbox is enabled |
| `filesystem_read` | Reads through the sandbox backend |
| `task_begin` / `plan_autofill` / `plan_next` / `plan_update` / `plan_remove_node` | Enter task mode, grow/edit a DAG skeleton, and drive execution |
| `task_status` | Inspect agent state and receive the next recommended DAG action |
| `plan_node_history` | Inspect DAG events attributed to one plan node |
| `summary_add` / `summary_list` / `summary_read` | Persist and inspect structured task summaries |
| `claim_add` / `claim_list` | Record and inspect verifiable claims with evidence |
| `debug_analyze` | Inspect task DAG, plan, state, context, mailbox, subagents, and per-node DAG activity |
| `filesystem_write` | Writes through the sandbox backend |
| `filesystem_list` | Lists through the sandbox backend |
| `ask` | Triggers a `user_ask` interrupt/resume flow |
| `message_send` | Emits a user-visible message through the interaction adapter |
| `memory_update` / `memory_list` / `memory_search` | Append, list, and search structured long-term memory entries |
| `subagent_create` | Attach mode runs a child runtime under the parent session; detach creates a pending manifest for the MVP runner |
| `subagent_wait` | Reads subagent manifest/status/result |
| `subagent_list` | Lists subagent manifests |
| `subagent_stop` | Marks a subagent as stopped |
| `compact` | Requests context compaction on the next graph pass |
| `skill_load` | Loads a `SKILL.md` file by name through sandbox when enabled |

## License

MIT
