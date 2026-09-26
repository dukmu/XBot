# `client_transport`

Provides the public HTTP/SSE client (`XBotClient`) as a normal capability of the
**client** carrier profile. The client profile is the local, foreground
composition: a terminal client plugin talks to a separately running
`xbot serve` process through this client, not through an in-process Engine.

- **Import/profile:** `client_transport`, client profile.
- **Tree id:** `client-transport`.
- **Source:** `XBotv2/client_transport/plugin.py`, `XBotv2/client.py`.
- **Injects/provides:** `client_launch` → `client_api` (`XBotClient`).
- **Owns:** the transport lifetime; it closes the client on context disposal.

This is a carrier plugin, not an Agent plugin. It has no Tools, no commands, and
no HTTP routes of its own.

## Registration

```python
class ClientTransportPlugin:
    name = "xbot.client_transport"
    inject = ["client_launch"]

    async def apply(
        self,
        ctx: Context,
        config: dict[str, JsonValue] | None = None,
    ) -> None:
        launch: ClientLaunch = ctx.require("client_launch")
        client = XBotClient(launch.base_url, uds_path=launch.uds_path)
        ctx.set("client_api", client)
        ctx.on("dispose", client.close)
```

`plugin = ClientTransportPlugin()` is the root export. `apply` is async here
because it awaits nothing else — the class form is still the object form, not
the class-plugin form, since `apply` receives Context.

The client is created from the launch facts and registered as `client_api`.
Cleanup is registered as a listener on the XCore `dispose` event, so the
transport closes when the owning context is destroyed. A dependent client
plugin should not close it independently.

## Launch facts

`ClientLaunch` is supplied by the CLI client host before the profile starts
(`XBotv2/application/client.py`):

```python
@dataclass(frozen=True, slots=True)
class ClientLaunch:
    data_dir: str
    base_url: str
    uds_path: str | None
    workspace: str
    session_id: str | None
    thread_id: str
    agent: str | None
    history_window: int | None = None
    history_retention: int | None = None
```

The client connects to `base_url` over HTTP, or to `uds_path` when a Unix domain
socket is configured.

## Public client surface

`XBotClient` (`XBotv2/client.py`) is the typed transport for the server's HTTP
API. It is the only supported way for a client plugin to reach the server; do
not hand-roll requests against the route paths.

- **Lifecycle:** `close()`, and `async with XBotClient(...)`.
- **Server:** `health()`, `hello(...)`.
- **Sessions:** `list_sessions()`, `open_session(...)`, `get_session(...)`,
  `fork_session(...)`, `delete_session(...)`, `close_session(...)`,
  `get_session_policy(...)`, `update_session_policy(...)`.
- **Threads:** `list_threads(...)`, `open_thread(...)`, `get_thread(...)`,
  `close_thread(...)`.
- **Conversation:** `list_messages(...)`, `list_trajectory(...)`,
  `read_artifact(...)`, `clear_history(...)`, `undo_history(...)`.
- **Commands and selection:** `list_commands(...)`, `run_command(...)`,
  `list_agents(...)`, `select_agent(...)`, `select_provider(...)`,
  `select_effort(...)`, `list_tools(...)`.
- **Jobs:** `list_jobs(...)`, `stop_job(...)`, `stop_all_jobs(...)`.
- **Config:** `list_plugin_config(...)`, `update_plugin_config(...)`.
- **Workspaces:** `list_workspaces()`.

Errors surface as `XBotClientError` carrying the server's `status_code` and
typed `ErrorResponse`.

## Where the client profile goes next

`textual-tui` (tree id, import name `tui`) is the other client-profile entry. It
consumes `client_api`, `client_launch`, and `commands`, and provides
`terminal_client` — the async foreground client the CLI awaits. See the
[client runtime reference](../client-runtime.md) for streamed events,
interactions, command discovery, and read-only views.

## Extension notes

- A third-party client plugin should inject `client_api` and register its own
  `terminal_client` only if it is the selected client; a display-only plugin
  should consume `client_api` and leave `terminal_client` to the TUI.
- Do not import the server package from a client plugin to reuse DTOs by
  convenience. Protocol models belong to their owning package and reach the
  client as typed responses.
- Do not close `client_api`. The transport plugin owns that, and closing it
  from a consumer breaks every other client plugin in the same composition.
