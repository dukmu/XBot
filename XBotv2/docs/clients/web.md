# Web Client

`XBotv2/web` is the browser client for protocol v3. It is intentionally a
separate TypeScript project. The Python server does not host its assets or
contain browser-specific routes.

## Protocol Boundary

The client performs the `/hello` handshake and uses the public session/thread
resource model described in [SDK contract](../protocol/sdk.md). Message submission and
server-initiated turns are separate SSE streams. Interaction responses use the
typed permission and user-input endpoints while the original stream remains
open.

The browser does not:

- execute Tools directly;
- read session files or plugin state;
- infer a provider `model_mode` when the API returns an empty value.

It does discover human command metadata. UI-owned lifecycle commands use typed
session resources; server-owned commands use the command compatibility route
and refresh only the resources declared by the result.

This keeps permission checks, sandboxing, caching, Hooks, and persistence in
the Agent runtime.

## Runtime UI

The workbench exposes durable Workspaces, persisted sessions and threads,
Agent/provider selection,
conversation history, reasoning disclosure, Tool call details, background
shell/subagent tasks, sequential permission and `ask_user` requests, history
mutations, fork, interrupt, cumulative session tokens, current context use, and
plugin status slots. It opens a bounded newest history page and fetches older
pages by cursor, renders injected context separately from human messages,
downloads persisted images/files through the artifact resource, and performs
regeneration through the atomic server operation. Todo is restored from the
plugin's typed projection rather than inferred from old Tool arguments.

The sidebar groups sessions by the server's Workspace registry. Workspace
rename, ordering, and removal plus session ordering, rename, and archive/restore
use typed resources. New members are prepended, manual session order survives
restart, and later activity does not reorder them. Archiving hides a session
from active groups without deleting its history or order slot; removing a
Workspace likewise does not delete its sessions. A session created with an
explicit directory first registers that existing directory so it does not
silently fall into an unmanaged path group.

New first reuses an unarchived blank session already accounted to the selected
Workspace. It creates a persisted session only when no such session exists.
`blank` is a server projection updated through the Workspace catalog stream after
turns and history mutations; the browser does not infer it from the currently
loaded history page.

Completed or stopped tasks remain briefly visible and are then removed from
the task dock. Failed tasks remain available for diagnosis. The server remains
the source of truth and tasks stay queryable through its API.

Navigation is non-destructive. Opening an active session attaches to its
existing runtime; switching the visible session aborts only the old event
subscription, not an in-flight message request or another client's turn. The
client tracks server reachability, session attachment, event-stream health,
and turn activity separately. If navigation fails, the previous identity,
timeline, and event subscription are restored.

The React-free session-event connection owns cancellation and retries
transient disconnects with bounded exponential backoff. HTTP failures that are
not marked retryable stop the connection and remain visible to the user.

Session and Workspace catalogs use a separate React-free Host connection.
`GET /sessions` and `GET /workspaces` return baseline cursors; the client starts
`GET /workspaces/events` from the older cursor and applies replayed changes over both
baselines. Resource frames received while a manual refresh is in flight are
reapplied after that baseline, so a slow response cannot erase a newer change.
An expired process cursor causes one fresh baseline instead of an infinite
retry loop. Another HTTP client can therefore create, rename, reorder, archive,
or delete a resource and the open sidebar updates without a page refresh.

## Hosting

During development Vite proxies `/api` to the loopback HTTP server, including
the long-lived Host and session SSE responses. Production
deployments serve the static build and reverse-proxy the same path. This avoids
adding CORS or static hosting behavior to Core. Because the current protocol
does not define authentication, the HTTP server remains loopback-only.

Commands and environment variables are documented in
[`web/README.md`](../../web/README.md).
