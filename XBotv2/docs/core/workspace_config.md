# Configuration

XBot configures plugins declaratively through plugin-tree YAML documents.
There is one bundled default tree and two overlay layers:

1. `XBotv2/xcore.yaml` - bundled default plugin tree (single config document)
2. `<data_dir>/config/plugins.yaml` - global user tree overlay
3. `<workspace>/.xbot/plugins.yaml` - workspace policy overlay

`data_dir` defaults to `~/.xbot` (`--data-dir` / `XBOT_DATA_DIR` overrides).
On first run the global user tree is seeded (DSH-style boot seed):
`config/plugins.yaml` is written when missing, so users edit that file
instead of the bundled tree.

Overlays merge per plugin id: the later entry's `config` is deep-merged into
the base entry, and `disabled` / `inject` / `isolate` are replaced.  New ids
mount additional plugins.  Unknown fields and invalid values stop startup
instead of being silently ignored.  Order in the file has no meaning: every
plugin with satisfied service dependencies loads.

```yaml
# <data_dir>/config/plugins.yaml — one entry per plugin id
- id: agents
  config:
    timeout_seconds: 600
- id: sample
  disabled: true
```

Workspace overlays are applied by the `workspace_instructions` plugin, which
also injects `<workspace>/AGENTS.md` into every model context build.  A
workspace overlay may disable any plugin including `workspace_instructions`
itself.

Agent runtime values are a config-service concern. Global, session, and
workspace `config.yaml` layers select the provider and configure instructions,
tool selectors, and policy. Application startup asks `ctx.settings` for the
resolved `RuntimeConfig`; there is no `application_config` plugin or launch
parameter entry in `xcore.yaml`.

Plugin `config` values are per-plugin; `permissions`, `sandbox`, and
`max_concurrent_subagents` live in the corresponding plugin entries
(`permissions`, `sandbox`, `jobs`).  Provider definitions are the ``llm``
plugin's tree config (``default`` + ``providers``) and the user context is
the ``config`` plugin's tree config (``user``) — there are no separate
``providers.yaml`` / ``user.yaml`` documents:

```text
<data_dir>/config/plugins.yaml   # global user tree overlay (llm/config entries)
```

Workspace Tool and Hook modules are trusted startup code. Configuration is
loaded when a thread starts; session policy changes are reloaded explicitly by
the policy API.

## Providers

Provider definitions live in the `llm` plugin's tree config (`default` +
`providers` in the `llm` entry of `xcore.yaml` or a `plugins.yaml` overlay —
seeded as part of the global user tree on first run). Runtime config selects
one by name; it does not duplicate model limits.

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
    input_modalities: [text, image]
```

`max_context_tokens` is required model capacity used for context accounting and
compaction. `max_output_tokens` is optional and is omitted from OpenAI-compatible
requests when absent. Anthropic Messages requires it, so Anthropic-compatible
providers must set it explicitly. Missing environment variables and unknown
provider names fail closed.

`input_modalities` declares the inputs accepted by that configured model. It
always includes `text`; add `image` only for a model endpoint that supports
image input. XBot rejects image messages before dispatch when the selected
provider does not declare that capability.

`thinking_enabled` is an explicit capability of a provider/model combination.
Verify it across a Tool call and the following response before enabling it;
XBot neither promotes reasoning blocks to assistant content nor silently falls
back to another thinking mode.

Transient provider connection failures, timeouts, HTTP 408/409/429 responses,
and HTTP 5xx responses are retried with exponential backoff before any response
chunk is emitted. The default retry limit is 16; once it is exhausted the
request fails with a provider retry error instead of continuing indefinitely.
These process-level controls deliberately do not belong to YAML configuration:

- `XBOT_PROVIDER_MAX_RETRIES`: retry count after the initial request; unset,
  defaults to `16`; `none` or `infinite` means unlimited only when explicitly set.
- `XBOT_PROVIDER_RETRY_BACKOFF_FACTOR`: delay factor in seconds, default `0.5`;
  retry `n` waits `factor * 2^(n-1)`.

Once a response chunk has been emitted, a stream failure is returned to Core
instead of replaying partial output.

## Agent Definitions

XBot ships two built-in Agent definitions (`default` and `Explorer`) registered
by the `agents` plugin, which also loads `<data_dir>/.agents/*.md`.  Workspace
definitions are discovered by the `workspace_instructions` plugin — the owner
of everything workspace-scoped — which registers `<workspace>/.agents/*.md`
as an overlay.  A same-named Markdown definition replaces the built-in (data
root wins over the built-in, workspace wins over the data root).  Disabling
`workspace_instructions` also disables workspace Agent definitions.

Agent frontmatter may select a provider/model and override generation or
context limits; these values do not belong in plugin config.  Agent
definitions are immutable during a turn. Run `/agent reload` while the thread
is idle to reload the Agent and workspace plugins and reapply the active
definition.  `/agent list` and `/agent use <name>` operate on the loaded
definitions.

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
