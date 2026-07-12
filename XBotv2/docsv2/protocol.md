# XBotv2 Protocol

## Transport

XBotv2 supports two transports:

- **Unix Domain Socket** (default for local TUI): auto-generated at
  `/tmp/xbotv2-{pid}.sock`. Server subprocess spawned and bound to it.
  No TCP port needed. Socket cleaned up on exit.

- **HTTP/SSE** (TCP, for remote): `--server URL` flag. Server binds
  `--bind`:`--port` (default `127.0.0.1:4096`). SSE streaming over TCP.

TUI transport is selectable at startup:
```bash
python -m xbotv2 tui                    # UDS (default)
python -m xbotv2 tui --server http://..  # HTTP remote
python -m xbotv2 server --bind 0.0.0.0   # server-only
```

## Session

### Modes

- `new`: create session, generate session_id if not provided
- `resume`: reconnect to existing session state on disk
- `new` with an existing explicit id returns HTTP 409; `resume` with a missing
  id returns HTTP 404. The server never changes modes implicitly.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/hello` | Client handshake |
| POST | `/sessions` | Open session (new/resume) |
| POST | `/sessions/{id}/messages` | Send message, receive SSE stream |
| POST | `/sessions/{id}/interrupt` | Cancel running turn |
| GET | `/sessions/{id}/commands` | List available commands (includes skills/tools) |
| POST | `/sessions/{id}/commands` | Execute server/skill/tool command |
| POST | `/sessions/{id}/interactions/permission-response` | Submit permission decision |
| POST | `/sessions/{id}/interactions/user-input` | Submit user input answer |
| POST | `/sessions/{id}/shutdown` | Close session |

## Command System

`GET /sessions/{id}/commands` returns unified command list with `kind` field:

```json
[
  {"name": "status", "kind": "server", "description": "Server status"},
  {"name": "shell", "kind": "tool", "description": "Execute shell"},
  {"name": "find-skills", "kind": "skill", "description": "Find skills"},
  {"name": "search", "kind": "mcp", "description": "MCP search"}
]
```

Kinds: `client` (local TUI only), `server`, `skill`, `tool`, `mcp`.

Skills are loaded via `register_dynamic_command()` from plugins during
`ON_SESSION_INIT`. Server `execute_command(kind="skill")` returns skill content.

## Stream Events

| Event | Data |
|---|---|
| `turn_started` | `{turn}` |
| `turn_finished` | `{turn}` |
| `assistant_message_delta` | `{content}` (includes reasoning via `additional_kwargs`) |
| `assistant_message` | `{content, tool_calls}` |
| `tool_call_delta` | `{index, id, name, args}` |
| `tool_calls_started` | `[{name, args, id}]` |
| `tool_result` | `{tool_call_id, content, status}` |
| `permission_request` | `{request_id, reason, tool_call}` |
| `permission_response_recorded` | `{request_id, decision}` |
| `usage` | `{input_tokens, output_tokens, total_tokens}` |
| `error` | `{code, message}` |

## Provider Events (internal)

`_model_response` event carries an aggregated `ModelResponse` with
content, tool_calls, usage_metadata, additional_kwargs (including
reasoning_content if present).
