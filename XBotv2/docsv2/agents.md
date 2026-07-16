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
└── threads/<thread-id>/
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

Subagent execution and background scheduling are not part of this registration
phase. Their implementation must reuse Engine and CoreStateStore, use bounded
results for the parent thread, and deliver background completion through the
runtime mailbox.
