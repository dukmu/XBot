# MCP plugin

The MCP plugin connects configured servers, discovers their Tools, and registers
those Tools and supported resource, prompt and completion bridges through the
standard Tool service. It depends on Tools, the model, interactions, session,
usage and loop state. Sampling and elicitation callbacks use the existing model
and interaction services. Calls pass the ordinary guards; registration does not
grant permissions.

`MCPConfig.servers` defaults to an empty mapping. Each `MCPServerConfig`
defaults to `enabled=true` and `required=false` and accepts transport-specific
options. Configuration is applied before connections are opened at application
initialization. For example:

```yaml
- id: mcp_plugin
  name: mcp_plugin
  config:
    servers:
      files:
        command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        required: false
```

The default local transport uses `command`, optional `env`, `cwd` and
`log_path`; `type: remote` uses `url`, optional `headers` and
`terminate_on_close` (default true). Timeout defaults to 30 seconds. Stderr
may be appended to `log_path`. The SDK owns stdio and streamable HTTP transport
behavior; server tool schemas are validated before registration.

Server Tools are named `mcp__<server>__<tool>` and owned under the
`mcp:<server>` namespace. Supported bridges use the same namespace with
`protocol_resources`, `protocol_prompts` and `protocol_complete` suffixes.
Connections, status and registrations are runtime state. Initialization failure
rolls back that server; a required server failure rolls back all connected
servers. Unregistration checks Tool identity before removing a name. Session
close disconnects servers and removes registrations. The plugin has no slash
commands or persisted conversation state. Public config models and the plugin
identifier are in `api/models.py`; client, callbacks and handlers are in `impl/`.
