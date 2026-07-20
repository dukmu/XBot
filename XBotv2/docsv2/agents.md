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
    permissions={"deny": [{
        "tool": "filesystem_(?:write|edit|patch|move|copy|delete|mkdir)"
    }]},
))
```

`mode` is `primary`, `subagent`, or `all`. An omitted provider inherits the
calling agent's configured provider. An omitted tool filter inherits the
resolved tool set. Child permissions are still bounded by the caller and the
workspace policy. Subagent runtimes do not load the `agents` plugin, so they
cannot create nested subagents.

## Thread Ownership

A session is the shared conversation workspace. Every primary agent or
subagent execution has a distinct thread ID within that session:

```text
data/sessions/<session-id>/
├── config.yaml
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

The built-in `agents` plugin loads `data/.agents/*.md`, then
`<workspace>/.agents/*.md` at startup. The filename is the Agent name; a
workspace definition replaces a same-named built-in definition. YAML
frontmatter contains configuration and the Markdown body is the Agent prompt:

```markdown
---
description: Review a focused change for correctness
mode: subagent
tools:
  - filesystem_read
  - search_text
permission:
  filesystem_write: deny
---
Report findings first and cite the relevant files.
```

Unknown fields fail startup. Accepted behavioral fields are `description`,
`mode`, `provider`, `model`, `temperature`, `max_output_tokens`,
`context_window`, `max_iterations` (or OpenCode's `steps`), `tools`,
`permission`, and `hidden`. `tools` may be an XBot selector list or an
OpenCode-style boolean mapping. The legacy `permissions` spelling remains
accepted with XBot's grouped rule schema. Standard `permission` keys are
canonical XBot tool names or wildcard patterns; XBot does not guess aliases such
as `bash` or `edit`. A `model` value may use `provider/model-id`. Credentials,
provider URLs, plugin configuration, sandbox roots, and Hook paths do not belong
in Agent Markdown.

`AGENTS.md` remains a standard workspace instruction file. It is reloaded for
each primary-agent and subagent model request and is never parsed as an Agent
definition.

Runtime variables in an Agent Markdown body use explicit fenced `var` blocks;
ordinary `${...}` Markdown and frontmatter descriptions remain literal.
Permission values retain their references until the permission system evaluates
them, so `paths: ${workspace}` keeps directory scope instead of becoming a
one-file regular expression.

Select a `primary` or `all` definition with `xbot --agent <name>`. The HTTP
session-open request exposes the same optional `agent` field. The selected name
and resolved definition are written to `thread.yaml`, so resume keeps the same
prompt, model settings, and tool policy even if the source Markdown later
changes. `/agent list` reports available definitions and `/agent use <name>`
switches the active Primary Agent for subsequent turns. The session, thread,
history, plugin state, and usage remain unchanged; prompt, model settings,
tools, and permissions are replaced together and the new resolved definition
becomes the thread's resume state. Switching is rejected while a turn is active.

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

The shipped `Explorer` definition has `mode: all` and exposes only read, list,
search, and `ask_user` tools. It can be selected as a primary Agent or delegated
to as a subagent; it cannot see filesystem writes, Shell, or subagent dispatch.
The shipped `default` definition is selected for a new primary thread when the
client does not choose an Agent. Explicit selection, resume metadata, and child
Agent definitions take precedence.
