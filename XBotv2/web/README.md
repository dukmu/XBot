# XBot Web

The Web client is an independent React/TypeScript application for the public
XBot protocol v3 HTTP/SSE API. It does not import Python runtime code. Typed
resources own machine operations; the separately discovered human command
catalog is used only for server-owned slash commands.

## Development

Start the existing XBot server:

```bash
uv run xbot serve \
  --workspace ./output \
  --provider minimax
```

Start the Web client in another terminal:

```bash
cd XBotv2/web
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to
`http://127.0.0.1:4096`. Set `XBOT_API_URL` when the XBot server uses another
local URL:

```bash
XBOT_API_URL=http://127.0.0.1:4100 npm run dev
```

From the repository root, `uv run xbot web` serves the compiled client with
Python, starts the API over an internal Unix socket, and opens the workbench.
Runtime Web mode does not invoke npm. Use
`--server http://127.0.0.1:4096` to connect to an existing API server or
`--no-open` when no desktop browser is available. The browser always uses
same-origin `/api`; the Python Web process performs the HTTP-to-UDS or
HTTP-to-HTTP proxying. `--server` and `--uds` are mutually exclusive, and the
generated socket is removed when Web mode exits.

## Production

```bash
npm run build
```

The ignored build output is written to `XBotv2/web_dist`. From the repository
root, run `uv build --all-packages --wheel` after the frontend build to include
the index and assets in the XBot wheel. Runtime Web mode never invokes npm; an
installed wheel serves its packaged assets. The Python server serves those
files and reverse-proxies `/api/*` while removing the `/api` prefix. XBot currently has no remote authentication contract, so Web
mode binds to loopback only. `VITE_XBOT_API_BASE` may select another same-origin
prefix at build time.

## Source Layout

- `src/api`: protocol v3 DTOs, HTTP resources, and incremental SSE decoding.
- `src/state`: the runtime reducer and connection lifecycle.
- `src/components`: resource navigation and interaction controls.
- `src/app`: the application shell.
- `src/styles`: the responsive workbench theme.

The sidebar keeps one page of workspaces and one page of sessions in memory.
Use the page controls to browse all sessions, one workspace, or archived
sessions; sidebar search applies to the current session page. Thread navigation
also holds one page, with previous, next, and refresh controls. The subagent
tree shows the current page; a child whose parent is on another page appears
at the root of that page. The active thread status refreshes automatically;
other threads show a snapshot until the user selects Refresh threads.

At widths of 820 pixels or less, the session sidebar opens over the conversation
and closes with Escape. Tool details open when a tool is selected; changing
sessions or threads clears the selection. Escape cancels queue editing or closes
an open settings menu without interrupting the running turn. The appearance
setting applies the same light, dark, or system preference across the workbench.

Message uploads use `media` entries with base64 data, MIME type and name;
ordinary files use `attachments`. Media references in history retain artifact
metadata and resolve through the artifact download endpoint.

The client consumes the server’s `kind`, `scope`, and `payload` event envelopes.
The state projection converts canonical conversation records and trajectory
messages into UI entries; reconnect starts from the snapshot event cursor.
The browser uses the thread summary for running state and statistics. Tool
execution stays inside the Agent runtime; the browser only answers interactions
and calls typed resource mutations.

The plugin configuration editor is schema-driven: `components/schemaForm.ts`
projects the catalog's pydantic JSON Schema (`$defs`/`$ref`, `anyOf` null
branches, enums, maps, arrays) into field descriptors and
`components/SchemaForm.tsx` renders them. It edits exactly one layer, so
`Clear` removes that key from the layer instead of writing the schema default,
and saving replaces the layer's whole configuration object. The plugin list is
narrow and stays visible while the form scrolls, plugins without a schema are
folded into one group, `Save plugin configuration` is enabled only after a real
change (with `Discard` to drop it), so a plugin layer is never edited from two
places on the same page.

## Verification

The current workbench changes were checked with focused component tests and
mock browser scenarios for queue editing on desktop and mobile and for closing
the mobile sidebar with Escape. Other older mock scenarios have not all been
migrated to the current protocol fixture.

The local Playwright real-server scenarios use the production loader and a
MockLLM. They cover streaming, tool results, media history and reattachment
while a turn is active, approval recovery and response, and undo/clear against
service history. They do not exercise an external model provider.

The optional built-assets thread test uses a running API and `xbot web`, not
Vite. Prepare a disposable data directory containing a persisted session with
more than 100 threads. Choose a parent ID on the first thread page and a child
ID on a later page whose `parent_thread_id` is that parent. Resume the main and
parent threads through the session API before the test; the child must already
exist or be created through the thread API. For example, with a prepared
fixture and variables `DATA_DIR`, `WORKSPACE`, `SESSION`, `PARENT`, and `CHILD`:

```bash
uv run xbot serve --data-dir "$DATA_DIR" --workspace "$WORKSPACE" --port 4097
# In another terminal, resume main and parent, then start the built Web server:
curl -X POST http://127.0.0.1:4097/sessions -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$SESSION\",\"thread_id\":\"main\",\"mode\":\"resume\"}"
curl -X POST "http://127.0.0.1:4097/sessions/$SESSION/threads" -H 'Content-Type: application/json' \
  -d "{\"thread_id\":\"$PARENT\",\"parent_thread_id\":\"main\",\"mode\":\"resume\"}"
uv run xbot web --server http://127.0.0.1:4097 --web-port 4174 --no-open
# In a third terminal, from XBotv2/web:
XBOT_THREAD_FIXTURE_SESSION="$SESSION" XBOT_THREAD_FIXTURE_PARENT="$PARENT" \
  XBOT_THREAD_FIXTURE_CHILD="$CHILD" npx playwright test --config playwright.built.config.ts
```

Set `XBOT_BUILT_WEB_URL` if the Web server uses a different address. The test
skips when fixture IDs are not supplied and is excluded from the default e2e
run. Shut down both servers afterward.

```bash
npm test
npm run e2e
npm run build
npm audit
```
