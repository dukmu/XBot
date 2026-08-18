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

Each provider is a vendor **adapter instance**: `protocol` selects the
protocol implementation (`openai` / `anthropic` / `mock`), `base_url` and
`api_key` identify the endpoint, `default_model` names the catalog entry used
when no explicit model is selected, and `models` is the catalog of specific
model configurations. The LLM interface is constructed as protocol
implementation → adapter instance → the selected model's specific config;
the selected model may come from an Agent definition (`model:`), and
unknown model names fail closed.

```yaml
default: minimax
providers:
  minimax:
    protocol: anthropic
    base_url: https://api.minimaxi.com/anthropic
    api_key_env: MINIMAX_API_TOKEN
    default_model: Minimax-M3
    models:
      - model: Minimax-M3
        temperature: 0.2         # optional; omitted -> provider default
        max_context_tokens: 1000000
        max_output_tokens: 32768 # required by the Anthropic Messages protocol
        thinking: adaptive       # adapter-owned; MiniMax: adaptive/disabled
        input_modalities: [text, image]
      - model: Minimax-M2.7
        max_context_tokens: 204800
        max_output_tokens: 32768
```

`max_context_tokens` is per-model capacity used for context accounting and
compaction. `max_output_tokens` is optional and is omitted from
OpenAI-compatible requests when absent; Anthropic Messages requires it, so
Anthropic-protocol models must set it explicitly. Missing environment
variables, unknown provider names, and unknown model names fail closed.

`temperature`, `reasoning_effort`, and `thinking` are optional per-model
sampling and reasoning settings; leaving one unset omits it from the request
so the provider default applies. `reasoning_effort` and `thinking` are
adapter-owned values: the adapter serializes them to the vendor wire format
(`reasoning_effort` top-level for OpenAI-compatible endpoints,
`extra_body.thinking` for Anthropic-style endpoints). MiniMax declares
`thinking: adaptive`; Claude-style endpoints use `thinking: enabled` plus
`budget_tokens` where required.

`effort` is an optional ordered list of adapter-owned reasoning effort tiers
the model advertises for `/effort` switching (e.g. `[low, medium, high]`);
when present, the active `reasoning_effort` must be one of them. Models
without `effort` do not advertise switchable tiers. Vendors with different
standards declare their own values (e.g. DeepSeek v4 `[low, high, max]`) and
the adapter serializes them unchanged.

`extra_body` carries vendor-specific request extras (Anthropic `extra_body` /
OpenAI-compatible top-level options) per model. Adapter-derived parameters
such as `reasoning_effort` and `thinking` are deep-merged underneath these
configured values, so each vendor declares its own standard (e.g.
`n_predict`, `repeat_penalty`, DeepSeek `thinking`) without runtime code
changes.

`input_modalities` declares the inputs accepted by that configured model. It
always includes `text`; add `image` only for a model endpoint that supports
image input. XBot rejects image messages before dispatch when the selected
model does not declare that capability.

`thinking`/`reasoning_effort` declare an explicit capability of a model.
Verify it across a Tool call and the following response before enabling it;
XBot neither promotes reasoning blocks to assistant content nor silently
falls back to another thinking mode.

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

## Soft Restart (`/reload`)

`/reload` is a session command whose semantics is a *system soft restart*:
it applies configuration changes to the live session without a process
restart.  The LLM service validates the merged provider catalog first
(fail-closed), then the `SOFT_RELOAD` event fans out — the loader
re-applies the global `<data_dir>/config/plugins.yaml` layer (changed
entries re-applied, new entries mounted, disabled entries unloaded), the
`workspace_instructions` plugin re-applies the workspace
`<workspace>/.xbot/plugins.yaml` overlay (also re-discovering
`<workspace>/.agents/*.md`), and the agents service re-binds the active
model client from the merged provider catalog.  `/agent reload` emits the
same event for its narrower scope.

The merged LLM provider catalog is validated before anything is touched: an
invalid provider section fails the whole reload with `config_invalid` and the
running configuration stays untouched.  A per-entry reload failure or a
missing active provider/model keeps the previous binding and is reported in
`errors` (last-good semantics, matching dsh settings snapshots); the session
never goes down because of a bad overlay.

Session-lifecycle entries declare `reloadable: false` in the plugin tree
(`session`, `persistence`, `jobs`, `agentloop`, `agents-service`) — the
loop, message stores, and running jobs hold their state and cannot be
re-applied live.  Overriding those entries in an overlay is reported as
restart-required and only takes effect after a process restart.

`AGENTS.md` is read before every context build and needs no reload.
`/agent reload` remains the focused command for Agent definitions; `/reload`
also re-discovers workspace Agents because it reloads
`workspace_instructions`.

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
