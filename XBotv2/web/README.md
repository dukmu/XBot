# XBot Web

The Web client is an independent React/TypeScript application for the public
XBot protocol v3 HTTP/SSE API. It does not import Python runtime code and does
not use the plugin-command compatibility endpoints.

## Development

Start the existing XBot server:

```bash
uv run xbotv2 serve \
  --data-dir XBotv2/data \
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

From the repository root, `./xbot web` serves the compiled client with Python,
starts the API over an internal Unix socket, and opens the workbench. Runtime
Web mode does not invoke npm. Use `--server http://127.0.0.1:4096` to connect to
an existing API server or `--no-open` when no desktop browser is available.
The browser always uses same-origin `/api`; the Python Web process performs the
HTTP-to-UDS or HTTP-to-HTTP proxying. `--server` and `--uds` are mutually
exclusive, and the generated socket is removed when Web mode exits.

## Production

```bash
npm run build
```

The build is written to `XBotv2/xbotv2/web_dist` and included in the Python
package. `xbotv2 web` serves those files and reverse-proxies `/api/*` while
removing the `/api` prefix. XBot currently has no remote authentication
contract, so Web mode binds to loopback only. `VITE_XBOT_API_BASE` may select
another same-origin prefix at build time.

## Source Layout

- `src/api`: protocol v3 DTOs, HTTP resources, and incremental SSE decoding.
- `src/state`: the runtime reducer and connection lifecycle.
- `src/components`: resource navigation and interaction controls.
- `src/app`: the application shell.
- `src/styles`: the responsive workbench theme.

The reducer is the only place that converts `ServerEvent` envelopes into UI
state. Tool execution stays inside the Agent runtime; the browser only answers
interactions and calls typed resource mutations.

## Verification

```bash
npm test
npm run e2e
npm run build
npm audit
```
