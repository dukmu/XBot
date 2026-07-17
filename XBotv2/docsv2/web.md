# Web Client

`XBotv2/web` is the browser client for protocol v3. It is intentionally a
separate TypeScript project. The Python server does not host its assets or
contain browser-specific routes.

## Protocol Boundary

The client performs the `/hello` handshake and uses the public session/thread
resource model described in [SDK contract](sdk.md). Message submission and
server-initiated turns are separate SSE streams. Interaction responses use the
typed permission and user-input endpoints while the original stream remains
open.

The browser does not:

- execute Tools directly;
- parse or invoke plugin command compatibility routes;
- read session files or plugin state;
- infer a provider `model_mode` when the API returns an empty value.

This keeps permission checks, sandboxing, caching, Hooks, and persistence in
the Agent runtime.

## Runtime UI

The workbench exposes persisted sessions and threads, Agent/provider selection,
conversation history, reasoning disclosure, Tool call details, background
shell/subagent tasks, sequential permission and `ask_user` requests, history
mutations, fork, interrupt, cumulative session tokens, current context use, and
plugin status slots.

Completed or stopped tasks remain briefly visible and are then removed from
the task dock. Failed tasks remain available for diagnosis. The server remains
the source of truth and tasks stay queryable through its API.

## Hosting

During development Vite proxies `/api` to the loopback HTTP server. Production
deployments serve the static build and reverse-proxy the same path. This avoids
adding CORS or static hosting behavior to Core. Because the current protocol
does not define authentication, the HTTP server remains loopback-only.

Commands and environment variables are documented in
[`web/README.md`](../web/README.md).
