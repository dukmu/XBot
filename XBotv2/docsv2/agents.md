# Agents And Subagents

Agent definitions are extensions. Plugins register them during `setup()` with
`PluginSetupContext.register_agent()`. Core owns definition uniqueness,
registration rollback, and later execution; plugins must not create a second
agent loop.

```python
ctx.register_agent(AgentDefinition(
    name="reviewer",
    description="Review a change for correctness and missing tests.",
    mode="subagent",
    prompt="Inspect the requested change and report findings first.",
    permissions={"deny": [{"tool": "filesystem_write"}]},
))
```

`mode` is `primary`, `subagent`, or `all`. An omitted provider inherits the
calling agent's configured provider. An omitted tool filter inherits the
resolved tool set. Permissions use the existing XBot permission schema and
canonical tool names.

## Thread Ownership

A session is the shared conversation workspace. Every primary agent or
subagent execution has a distinct thread ID within that session:

```text
data/sessions/<session-id>/
├── policy.yaml
├── threads.jsonl
└── threads/<thread-id>/
    ├── thread.yaml
    ├── state/messages.jsonl
    ├── state/usage.yaml
    ├── state/plugin_states/
    ├── state/artifacts/
    └── logs/mailbox.jsonl
```

Session policy is shared. Conversation history, usage, plugin state, cached
artifacts, and mailbox audit records are thread-local. Existing sessions with
the legacy `state/` layout remain readable as the `agent` thread; new writes
use the thread layout.

## Workspace Definitions

The built-in `agents` plugin loads `.xbot/agents/*.md` at startup. The filename
is the Agent name, YAML frontmatter contains the definition, and the Markdown
body is its prompt:

```markdown
---
description: Review a focused change for correctness
mode: subagent
tools:
  - filesystem_read
  - search_text
permissions:
  deny:
    - tool: filesystem_write
---
Report findings first and cite the relevant files.
```

Unknown fields fail startup. XBot uses `provider`, `permissions`, and canonical
Tool names rather than translating provider- or client-specific aliases.

Select a `primary` or `all` definition with `xbotv2 --agent <name>`. The HTTP
session-open request exposes the same optional `agent` field. The selected name
is written to `thread.yaml`, so resume restores the same Primary Agent without
requiring the client to send it again. `/agent [list|status]` reports the active
and registered definitions; changing the Agent requires a new thread rather
than mixing two Primary prompts into one history.

## Execution

The Agents plugin registers `task(agent, prompt, background=false)`. Blocking
mode waits for the child final response. Background mode returns an
`agent-task-*` ID immediately, emits `task_updated` snapshots, and places the
final result in the parent runtime mailbox. `list_agent_tasks` and
`stop_agent_task` inspect and stop background work.

Both modes use `SubagentManager` and the normal `bootstrap()`/Engine path. Child
permissions are intersected with parent permissions, so an Agent definition can
restrict but cannot expand its caller's authority. Child results pass through
the standard ToolResult cache; full child history remains in its thread. Active
background children are cancelled when the live session closes.

A blocking child shares the parent turn's live interaction sink, so its
`ask_user` and permission requests use the normal ordered C/S interaction flow.
A background child may use that sink only while the originating turn remains
connected; later interactive requests fail closed instead of waiting without a
client owner.
