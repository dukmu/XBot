# Context construction

The builder compiles canonical conversation and registered prompt components
into provider-neutral messages. It owns component ordering and artifact path
resolution, but no provider requests, turn tasks, persistent history or Agent
selection. The loader imports `impl/plugin.py`.

`api/contracts.py` declares component values, `BuiltContext`, the
`PromptComponentRegistry` registration protocol and `ContextCompiler` projection
protocol. Their combined `ContextBuilderService` contract types the shared
`CONTEXT_BUILDER` ServiceKey. `api/events.py` owns the context-build request and
EventKey declarations associated with their actual payload types.
Cross-plugin consumers import these modules; the package root has no facade.
`impl/builder.py` contains the component registry, assembly and compiler.

The plugin waits for `runtime_log` and `artifacts`, publishes `context_builder`,
and handles `BUILD_CONTEXT`. The loop publishes `CONTEXT_BUILD_INPUTS_READY`
before requesting that build. The handler assembles components, emits
`CONTEXT_COMPONENTS_BUILT` for contributors, then compiles the result. Prompt
registration and compaction use narrow protocols implemented by that same
builder; consumers never instantiate a second builder at runtime.

System stages are ordered `system_prefix`, `system_instructions`,
`system_rules`, `context_suffix`. Stable runtime/Agent/memory components precede
canonical history. Registered prompt fragments retain their source and logical
path. Compilation maps user/runtime notices, assistant messages, summaries and
tool outcomes to provider values. Media parts carry canonical ArtifactRef values;
media and attachments require the active ArtifactStore and resolve logical
references to absolute model-facing paths. The builder does not read media bytes
or replace artifact references in persisted data. Provider adapters encode image
bytes at the request boundary. Audio and video can be represented, but current
adapters reject them before sending.

Registry entries are runtime-only and removed through the registering owner's
lifecycle. There is no builder state namespace or independent configuration.
The build request supplies resolved Agent selection, user identity, memory,
sandbox summary, runtime paths and turn number. Empty prompt text is omitted.
No model prefix cache is owned here: prompt stage/source/order changes alter
request content and can affect the provider's prefix caching naturally.

```python
from XBotv2.context_builder.api.contracts import CONTEXT_BUILDER, InlinePromptComponent

# From the plugin's owning apply callback, after context_builder is injected:
component = InlinePromptComponent(
    stage="system_rules", source="review_policy", text="Explain verification."
)
# builder = ctx.require(CONTEXT_BUILDER)
# builder.register_component("review_policy", component)
# ctx.dispose(lambda: builder.unregister_owner("review_policy"))
```

Owner tests exercise component order, canonical message projection, artifact
paths and missing-store failures. Composed loop and compaction tests exercise
the production event handler and the shared compiler service.

Before compiling file paths, `pin_components(built_context, resources,
artifacts=...)` acquires the referenced artifacts into the caller's
`AsyncExitStack`. The synchronous compiler only renders already prepared
paths. `ContextBuildRequest.resources` is the candidate's runtime resource
scope, not part of any provider request or persistent message. Attachment
instructions carry both the logical `artifact_id` and the current absolute
path. Logical references survive restore; process cache paths do not.
