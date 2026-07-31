# Agent Client Protocol compatibility

XBot exposes an ACP v1 Agent over standard input and standard output:

```bash
xbot acp --data-dir /path/to/data --provider default
```

The adapter uses the official `agent-client-protocol` Python SDK. ACP is an
external adapter: it translates protocol requests and session updates to
XBot's existing runtime without changing Engine, Tool, Plugin, Hook, or
persistence contracts.

## Resource mapping

One ACP session maps to the `agent` thread of one persisted XBot session.
Subagent threads remain internal and are represented through their normal tool
and task updates. ACP session IDs are XBot session IDs.

The ACP request `cwd` is the XBot workspace. A resumed or loaded session must
use the workspace recorded when it was created.

## Compatibility matrix

| ACP v1 feature | Status | XBot behavior |
| --- | --- | --- |
| Initialization and capability negotiation | Supported | Protocol v1 only |
| `session/new` | Supported | Creates the main `agent` thread |
| `session/resume` | Supported | Restores state without replay |
| `session/load` | Supported | Replays messages, thoughts, tools, and plans |
| `session/list` | Supported | Optional `cwd` filtering, no pagination |
| `session/close` | Supported | Cancels work and releases the runtime |
| Text prompts | Supported | Runs one normal XBot turn |
| Embedded text and resource links | Supported | Structured prompt context |
| Image prompts | Supported | Base64 payload is stored as a session media artifact |
| Audio and binary resources | Unsupported | Not advertised |
| Assistant and thought streaming | Supported | Separate ACP chunks |
| Tool call status | Supported | Parsed call and final result |
| Tool argument delta streaming | Not exposed | Parsed call is authoritative |
| Permission requests | Supported | Once, session, or deny |
| Structured user input | Partial | Flat form with optional enum choices |
| Cancellation | Supported | Interrupts the active turn |
| Usage updates | Supported | Context usage and context window |
| Agent plan | Supported | Todo results become ACP plans |
| Mailbox and background updates | Supported | Forwarded while the session is active |
| Slash command discovery | Supported | Plugin commands only |
| Server slash commands | Supported | Existing command handler |
| Prompt slash commands | Supported | Normal XBot prompt |
| Session config options | Supported | Main Agent and Provider/model |
| Client filesystem and terminal | Not used | XBot workspace and sandbox apply |
| Session-provided MCP stdio servers | Supported | Session-scoped MCP plugin config |
| Session-provided MCP HTTP servers | Supported | Streamable HTTP transport |
| Session-provided MCP SSE servers | Unsupported | Not advertised |
| Additional workspace roots | Unsupported | Not advertised |
| `session/fork` | Supported (unstable) | Copies persisted XBot state into a new session |
| `session/delete` | Unsupported | Current official Python SDK does not route this method |
| Authentication | Unsupported | No methods advertised |
| ACP v2 | Unsupported | Outside this adapter contract |

## Transport

ACP JSON-RPC is the only data written to stdout. Clients should launch
`xbot acp` directly rather than the TUI or terminal modes. Runtime logs use
XBot's normal logging configuration and must target stderr or a log file.

## Known boundaries

- ACP clients see the main conversation, not XBot's internal thread tree.
- Loading replays persisted conversation state. Runtime-only mailbox and
  interaction requests are not replayed.
- Process disconnect releases live runtimes; persisted sessions remain
  available for resume or load.
- Session-provided MCP configuration is an in-memory overlay and never rewrites
  global, workspace, or persisted configuration.
- `session/fork`, `session/resume`, and `session/close` use the official Python
  SDK's unstable protocol routes. Clients must enable those routes.
- The ACP schema includes `session/delete`, but the current official Python SDK
  does not expose it through its Agent router. XBot does not advertise a method
  that its selected SDK cannot dispatch.
