# Configuration

XBot resolves one validated runtime configuration from these UTF-8 YAML files:

1. `data/config/config.yaml` - global defaults
2. `data/sessions/<session-id>/config.yaml` - session choices and approvals
3. `<workspace>/.xbot/config.yaml` - workspace policy

Later layers have higher priority. Mappings merge recursively; scalar values and
lists replace the lower-layer value. An explicit empty list therefore clears a
lower-layer list. Unknown fields and invalid values stop startup instead of
being silently ignored.

```yaml
provider: default
max_concurrent_subagents: 4

tool_results:
  max_inline_chars: 12000
  preview_chars: 4000

plugins:
  agents:
    enabled: true
    config: {}
  sample:
    enabled: false

plugin_paths:
  - .xbot/plugins

workspace_tools:
  - target: tools/release.py:TOOLS

hooks:
  - stage: before_tool_call
    target: hooks/check_tool.py:check_tool

permissions:
  allow:
    - tool: filesystem_(?:read|write)
      paths: ${workspace}
  ask:
    - tool: .*
  deny: []

sandbox:
  enabled: true
  network: false
  workspace_read: allow
  workspace_write: allow
  external_read: ask
  external_write: deny
  resources: []
```

Workspace `plugin_paths` are relative to the workspace and may not escape it.
The standard workspace plugin root is `.xbot/plugins`; additional roots remain
supported when explicitly configured.

`workspace_tools` explicitly loads trusted Tool exports from `.xbot/tools`.
Each target uses `tools/module.py:export` syntax. The export is either one
`xbotv2.api.Tool` or a non-empty list/tuple of Tools, conventionally named
`TOOLS`. They enter the normal ToolRegistry, permission, Hook, result-cache,
and ToolResult execution path under the `workspace` namespace. No ordinary
functions are scanned implicitly.

Workspace Hook targets use `hooks/module.py:callback` and must stay inside
`.xbot/hooks`. A Hook remains explicitly associated with a stage in `hooks`;
filenames do not imply lifecycle stages or ordering. Workspace Tool and Hook
modules are trusted startup code. Configuration is loaded when a thread starts;
session policy changes are reloaded explicitly by the policy API.

## Providers

Provider definitions live only in `data/config/providers.yaml`. Runtime config
selects one by name; it does not duplicate model limits.

```yaml
default: minimax
providers:
  minimax:
    provider: anthropic
    model: MiniMax-M3
    base_url: https://example.invalid/anthropic
    api_key_env: MINIMAX_API_KEY
    max_context_tokens: 200000
    max_output_tokens: 32768 # required by the Anthropic Messages protocol
```

`max_context_tokens` is required model capacity used for context accounting and
compaction. `max_output_tokens` is optional and is omitted from OpenAI-compatible
requests when absent. Anthropic Messages requires it, so Anthropic-compatible
providers must set it explicitly. Missing environment variables and unknown
provider names fail closed.

`thinking_enabled` is an explicit capability of a provider/model combination.
Verify it across a Tool call and the following response before enabling it;
XBot neither promotes reasoning blocks to assistant content nor silently falls
back to another thinking mode.

## Agent Definitions

`data/.agents/*.md` and `<workspace>/.agents/*.md` define Agents. Workspace
definitions with the same filename override built-ins. Agent frontmatter may
select a provider/model and override generation or context limits; these values
do not belong in runtime `config.yaml`.

Agent definitions are immutable during a turn. Run `/agent reload` while the
thread is idle to reload the Agent plugin and reapply the active definition.
`/agent list` and `/agent use <name>` operate on the loaded definitions.

`<workspace>/AGENTS.md` is different: the `workspace_instructions` plugin reads
it before every context build, so edits apply to the next model request without
an Agent reload.

## Runtime Variables

Each thread receives one immutable runtime-variable mapping:

| Variable | Value |
|---|---|
| `${workspace}` | Active workspace root |
| `${data_dir}` | Runtime data root |
| `${config_dir}` | Global configuration directory |
| `${custom_config_dir}` | Workspace `.xbot` directory |
| `${session_dir}` | Shared session directory |
| `${thread_dir}` | Current thread directory |
| `${state_dir}` | Current thread state directory |
| `${plugin_states}` | Plugin-state directory |
| `${artifacts}` | Thread artifact directory |
| `${tool_results}` | Cached Tool-result directory |

Permission paths and sandbox resources expand these variables. Markdown prompt
sources expand a variable only when it is the sole content of a `var` fence:

````markdown
```var
${workspace}
```
````

References outside such a fence remain literal Markdown. Unknown variables fail
loading.
