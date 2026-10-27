# Workspaces

Workspaces owns process-scoped workspace records, ordering, archive membership and directory browsing. It depends on runtime logging, sessions, state and the workspace root, and provides `WORKSPACES`, `WORKSPACE_EVENTS` and `WORKSPACE_DIRECTORIES`. Startup ensures the configured root is registered. Workspace membership, archive state and order are stored in the process database; session title and workspace facts remain session-owned. Replay frames, sequence and subscribers are process-local. The replay window defaults to 512 frames and 8 MiB of UTF-8 JSON serialized frame data, including sequence and change metadata. These are process-local constructor defaults, not user configuration. An oversized frame advances the cursor but is not retained. Subscribers hold a cursor and a coalesced wakeup; expired cursors require a fresh snapshot. Snapshot cursors are captured before reading snapshot data so a subsequent subscription cannot silently miss a concurrent event.

The plugin observes session resource changes to maintain membership, emits workspace resource, ordering and archive events after changes, and publishes catalog events to a bounded replay stream. Its HTTP router serves workspace and directory resources and an SSE event stream. It contributes no commands, tools, prompt text or plugin-specific configuration.

```python
from XBotv2.workspaces.api.contracts import WORKSPACES

workspaces = ctx.require(WORKSPACES)
listing = await workspaces.list()
```

Large workspace collections are not yet paginated.
