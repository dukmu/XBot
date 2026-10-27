# Workspaces

Workspaces owns process-scoped workspace records, ordering, archive membership and directory browsing. It depends on runtime logging, sessions, state and the workspace root, and provides `WORKSPACES`, `WORKSPACE_EVENTS` and `WORKSPACE_DIRECTORIES`. Startup ensures the configured root is registered. Workspace snapshots are stored in the `workspaces` state namespace; replay frames, sequence and subscribers are process-local. The replay buffer defaults to 512 events and rejects expired cursors.

The plugin observes session resource changes to maintain membership, emits workspace resource, ordering and archive events after changes, and publishes catalog events to a bounded replay stream. Its HTTP router serves workspace and directory resources and an SSE event stream. It contributes no commands, tools, prompt text or plugin-specific configuration.

```python
from XBotv2.workspaces.api.contracts import WORKSPACES

workspaces = ctx.require(WORKSPACES)
listing = await workspaces.list()
```
