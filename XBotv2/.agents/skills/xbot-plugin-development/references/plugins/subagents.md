# Subagents

Subagents provides Agent-facing child-thread collaboration through the standard tool registry. It depends on the current session, sessions service, Agent catalog, session launch settings, no-plugins setting, jobs and tools. The launcher uses the existing session lifecycle to create or reopen children, submit inputs and deliver messages; it does not own a second runtime or mailbox. Launcher locks are temporary runtime state, while the session owner retains the child thread and history and jobs holds execution state.

The plugin registers `spawn_subagent`, `send_message`, `followup_task`, `list_subagents`, `wait_subagent`, `read_subagent` and `cancel_subagent`. It contributes the visible Agent catalog on `CONTEXT_COMPONENTS_BUILT`. There are no slash commands or HTTP routes. Cancellation stops the job while leaving the child thread available.

`SubagentsConfig.timeout_seconds` defaults to 600 seconds and is applied at registration to `spawn_subagent`; the other tools use their standard timeout. For example:

```python
from XBotv2.subagents.api.contracts import SubagentsConfig

config = SubagentsConfig(timeout_seconds=300)
```

See `XBotv2/subagents/doc/design.md` and `XBotv2/subagents/api/` for the contracts.
