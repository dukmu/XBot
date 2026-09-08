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

## Plugin packages

| Plugin | Page |
|---|---|
| `config` | [config.md](config.md) |
| `persistence` | [persistence.md](persistence.md) |
| `usage` | [usage.md](usage.md) |
| `agents` | [agents.md](agents.md) |
| `session` | [session.md](session.md) |
| `jobs` | [jobs.md](jobs.md) |
| `commands` | [commands.md](commands.md) |
| `llm` | [llm.md](llm.md) |
| `agentloop` | [agentloop.md](agentloop.md) |
| `context-builder` | [context-builder.md](context-builder.md) |
| `prompts` | [prompts.md](prompts.md) |
| `sandbox` | [sandbox.md](sandbox.md) |
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

## Carrier-only plugin packages

| Plugin | Page |
|---|---|
| `workspaces` | [process-workspaces.md](process-workspaces.md) |
| `acp-plugin` | [acp-plugin.md](acp-plugin.md) |
| `server` | [server.md](server.md) |

## Detailed facets

These pages describe substantial facets of the plugin above them; they are
not separate tree entries or plugin packages:

| Owner | Facet pages |
|---|---|
| `agents` | [catalog](agent-catalog.md), [runtime](agents.md), [HTTP](server-routes-agents.md) |
| `agentloop` | [Tool service](tools.md) |
| `llm` | [commands](llm-commands.md), [HTTP](server-routes-llm.md) |
| `persistence` | [process reader](process-persistence.md) |
| `session` | [process manager](process-sessions.md), [HTTP](server-routes-session.md) |
| `server` | [core HTTP](server-routes-core.md) |
| `workspaces` | [HTTP](server-routes-workspaces.md) |
| `jobs` | [HTTP](server-routes-jobs.md) |
| `config` | [HTTP](server-routes-config.md) |

> Carrier facets are **not** separate plugins or Agent Tools. The owning root
> plugin activates them only when their declared carrier services exist.
