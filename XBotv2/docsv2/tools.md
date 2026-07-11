# Built-in Tools

Core registers these tools without plugins:

| Tool | Execution | Purpose |
|---|---|---|
| `shell` | sandboxed, sequential | Run a command |
| `filesystem_read` | sandboxed, parallel | Read UTF-8 text with metadata |
| `filesystem_write` | sandboxed, sequential | Write, patch, or replace text |
| `filesystem_list` | sandboxed, parallel | List directory entries |
| `search_text` | sandboxed, parallel | Search UTF-8 text by regular expression |
| `find_files` | sandboxed, parallel | Find files by glob |
| `send_message` | host, sequential | Emit a non-blocking client message |
| `ask_user` | host, sequential | Wait for client input |

Tools return `ToolResult`. It separates model-visible text from structured data,
errors, artifacts, and client events. The dispatcher honors each registry
entry's `sandbox_mode`; host tools are never injected with a sandbox backend.

Disabling the session sandbox is an explicit policy choice. Permission checks
still run before every tool call.
