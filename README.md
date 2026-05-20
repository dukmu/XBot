# XBot Hermes

Hermes is a lightweight, single-user local agent built around a high-quality context loop: explicit system prompt construction, permission interrupts, active ask, context compression, and a future context tree with rewind and subagents.

The current codebase is in an early development stage. The main loop, LangGraph ReAct flow, permission checks, config loading, and basic tool surface exist. Some planned capabilities, such as full subagents, mailbox, persistent context tree, and tool-result cache hooks, are documented as design targets rather than complete runtime behavior.

## Design Intent

Hermes is meant to be a personal agent, not a multi-tenant service. The main design goals are:

- **Single-user local runtime**: one user, one local workspace, one default session.
- **Explicit model context**: `system prompt + system state + message chain + optional think blocks`.
- **Permission-first tools**: tool calls go through allow/deny/ask rules; sensitive actions interrupt for confirmation.
- **Active ask**: the agent can pause and ask the user for missing intent or decisions.
- **Context compression**: when context grows too large, older history becomes a compacted summary node.
- **Context tree**: future message history is a tree, supporting branches, compacted nodes, rewind, and tree inspection.
- **Subagents**: future synchronous and asynchronous workers can inherit or start fresh context.
- **Tool cache hooks**: large tool results should be cached and referenced instead of copied wholesale into context.

See [docs/architecture.md](./docs/architecture.md) for the full Hermes architecture.

## Current Capability Status

| Capability | Status |
|------------|--------|
| LangGraph ReAct loop | Implemented |
| Terminal runtime | Implemented |
| Provider config | Implemented |
| Permission allow/deny/ask | Implemented |
| Permission interrupt confirmation | Basic implementation |
| Active ask | Basic implementation |
| Context compression | Partial |
| Tool result cache hooks | Basic in-memory implementation |
| Context tree and rewind | Planned |
| Subagents | Placeholder |
| Mailbox | Planned |
| SQLite persistence | Code exists, not default |
| Runtime persistence default | `InMemorySaver` / `InMemoryStore` |

## Repository Layout

```text
./
├── main.py                    # Terminal entry point
├── pyproject.toml             # Project metadata and uv dependencies
├── README.md
├── xbot/
│   ├── models.py              # Pydantic models and state types
│   ├── config.py              # Configuration loading
│   ├── permissions.py         # Permission system
│   ├── tools.py               # Built-in tools and placeholders
│   ├── skills.py              # Skill discovery and loading
│   ├── llm.py                 # LLM factory
│   ├── graph.py               # LangGraph state graph
│   ├── checkpointer.py        # SQLite persistence target
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
    │   ├── agent.yaml
    │   ├── permissions.json
    │   ├── personality_template.md
    │   └── user.yaml
    ├── sessions/
    │   └── default/
    │       ├── workspace/
    │       ├── cache/
    │       └── subagents/
    └── personality/
        └── default/
            ├── AGENT.md
            ├── MEMORY.md
            ├── agent.yaml
            ├── permissions.json
            ├── jobs.json
            └── skills/
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
name: "minimax"
type: "anthropic"
base_url: "https://api.minimaxi.com/anthropic"
api_key: "${ANTHROPIC_API_KEY}"
model: "Minimax-M2.7"
max_concurrent: 2
```

Configure the user in `data/config/user.yaml`:

```yaml
user_id: "local_user"
user_name: "Alice"
platform: "local"
session_type: "private"
```

Configure permissions in `data/config/permissions.json` or `data/personality/default/permissions.json`:

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
```

Note: `--disable-inmemory` is currently a declared flag, but the runtime still defaults to in-memory persistence.

## Runtime Graph

Current graph:

```text
START -> agent -> tools -> agent -> END
              \-> compress -> agent
```

Permission confirmation is handled inside the tools node via LangGraph interrupt/resume.

## Built-in Tools

| Tool | Current behavior |
|------|------------------|
| `shell` | Mocked for safety |
| `filesystem_read` | Reads files under the workspace path |
| `filesystem_write` | Mocked for safety |
| `filesystem_list` | Lists workspace files |
| `ask` | Placeholder for active ask interrupt |
| `message_send` | Prints a message to terminal |
| `memory_update` | Appends to `MEMORY.md` |
| `subagent_create` | Creates placeholder subagent workspace and ID |
| `subagent_wait` | Placeholder |
| `subagent_list` | Lists subagent directories |
| `subagent_stop` | Placeholder |
| `compact` | Placeholder trigger response |
| `skill_load` | Loads a `SKILL.md` file by name |

## License

MIT
