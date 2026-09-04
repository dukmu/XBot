# Built-in Plugin Details

Each page describes one XBot built-in plugin using a consistent format:

- **identity/profile** — tree `id`, import `name`, process profile;
- **source** — key source files with line references;
- **inject/provides** — activation dependencies and services published;
- **events** — events subscribed, emitted, or short-circuited;
- **data models** — public classes, dataclasses, Protocols with real code snippets;
- **API surface** — Tool signatures, commands, operations, HTTP routes;
- **extension** — typical `apply()` pattern;
- **cross-references** — depends-on / depended-by;
- **pitfalls** — real-world mistakes observed in development.

These are quick references, not a replacement for the package's public
`__init__.py`, protocol models, or tests.

## Agent capabilities (27 plugins)

| Plugin | Page |
|---|---|
| `config` | [config.md](config.md) |
| `persistence` | [persistence.md](persistence.md) |
| `usage` | [usage.md](usage.md) |
| `agent-catalog` | [agent-catalog.md](agent-catalog.md) |
| `session` | [session.md](session.md) |
| `jobs` | [jobs.md](jobs.md) |
| `commands` | [commands.md](commands.md) |
| `llm` | [llm.md](llm.md) |
| `tools` | [tools.md](tools.md) |
| `agentloop` | [agentloop.md](agentloop.md) |
| `agent-runtime` | [agent-runtime.md](agent-runtime.md) |
| `llm-commands` | [llm-commands.md](llm-commands.md) |
| `context-builder` | [context-builder.md](context-builder.md) |
| `prompts` | [prompts.md](prompts.md) |
| `sandbox` | [sandbox.md](sandbox.md) |
| `permission-request` | [permission-request.md](permission-request.md) |
| `permissions` | [permissions.md](permissions.md) |
| `coretools` | [coretools.md](coretools.md) |
| `subagents` | [subagents.md](subagents.md) |
| `goal` | [goal.md](goal.md) |
| `todolist` | [todolist.md](todolist.md) |
| `skills` | [skills.md](skills.md) |
| `mcp-plugin` | [mcp-plugin.md](mcp-plugin.md) |
| `content-cache` | [content-cache.md](content-cache.md) |
| `compact` | [compact.md](compact.md) |
| `browser` | [browser.md](browser.md) |
| `token-manager` | [token-manager.md](token-manager.md) |
| `workspace-instructions` | [workspace-instructions.md](workspace-instructions.md) |
| `interactions` | [interactions.md](interactions.md) |

## Process carriers (11 plugins)

| Plugin | Page |
|---|---|
| `process-persistence` | [process-persistence.md](process-persistence.md) |
| `process-sessions` | [process-sessions.md](process-sessions.md) |
| `process-workspaces` | [process-workspaces.md](process-workspaces.md) |
| `acp-plugin` | [acp-plugin.md](acp-plugin.md) |
| `server` | [server.md](server.md) |
| `server-routes-core` | [server-routes-core.md](server-routes-core.md) |
| `server-routes-session` | [server-routes-session.md](server-routes-session.md) |
| `server-routes-workspaces` | [server-routes-workspaces.md](server-routes-workspaces.md) |
| `server-routes-jobs` | [server-routes-jobs.md](server-routes-jobs.md) |
| `server-routes-agents` | [server-routes-agents.md](server-routes-agents.md) |
| `server-routes-llm` | [server-routes-llm.md](server-routes-llm.md) |
| `server-routes-config` | [server-routes-config.md](server-routes-config.md) |

> Process plugins are **not** Agent Tools. They are not needed in an
> Agent-only profile unless the application explicitly requires them.
