# `caption`

Names a session. After the first user message the plugin issues one independent
model request to derive a short human-facing title, stores it on the thread
metadata, and grants the main Agent a Tool to read or overwrite that title.

The caption request is a side channel: it never appends to the conversation and
never blocks the main turn. Captioning is skipped for subagent threads, which
are named by their parent session.

- **Import/profile:** `caption`, Agent profile.
- **Source:** `XBotv2/caption/plugin.py`, `contracts.py`, `service.py`,
  `tools.py`.
- **Injects/provides:** `tools`, `model`, `loop_state`, `session`,
  `agent_options`, `usage` → `caption` (`CaptionService`).
- **Subscribes to events:** `context/before-build`
  (`Events.BEFORE_CONTEXT_BUILD`), registered with `prepend=True`.
- **Tools:** `caption` (Agent-visible only when `allow_access` is true and the
  thread is not a subagent).

## Registration

The root export is `plugin = CaptionPlugin()` in `XBotv2/caption/plugin.py`.
There is no HTTP route and no slash command; the plugin is entirely a context
observer plus one Tool.

```python
class CaptionPlugin:
    inject = ["tools", "model", "loop_state", "session", "agent_options", "usage"]
    name = "caption"
    Config = CaptionConfig

    def apply(self, ctx: Context, config: CaptionConfig) -> None:
        is_subagent = ctx.agent_options.is_subagent
        service = CaptionService(
            events=ctx,
            model=ctx.model,
            state=ctx.loop_state.metadata,
            usage=ctx.usage,
            session_id=ctx.session.session_id,
            config=config,
            is_subagent=is_subagent,
        )
        # An independent observer: it never short-circuits and must run even
        # when another listener answers BEFORE_CONTEXT first.
        ctx.on(Events.BEFORE_CONTEXT_BUILD, service._on_before_context, prepend=True)
        if config.allow_access and not is_subagent:
            ctx.tools.register(build_caption_tool(service))
        ctx.set("caption", service)
```

`prepend=True` is load-bearing. A short-circuiting listener may answer
`context/before-build` without producing a context, and the caption observer has
to run before that happens; the plugin deliberately does not return a result of
its own.

## Configuration

```python
class CaptionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto: bool = True
    allow_access: bool = True
    max_chars: int = Field(default=60, ge=10, le=200)
    output_tokens: int = Field(default=96, ge=8, le=256)
```

- `auto` — whether the first user message triggers an independent caption
  request. On by default so every session gets a readable title. A per-request
  model override (a temporary binding) suppresses it.
- `allow_access` — whether the main Agent receives the `caption` Tool. When
  disabled the title stays human-facing only.
- `max_chars` — bound applied when normalizing the generated title.
- `output_tokens` — budget for the caption request. A thinking model can spend
  it on reasoning, so `caption/service.py` derives a deterministic title from
  the first user message when the model emits no content.

## Public data models (`caption/contracts.py`)

```python
class CaptionRequest(BaseModel):
    messages: tuple[ConversationMessage, ...]
    current_title: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaptionResult(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaptionTitleError(ValueError):
    """A requested title cannot be normalized to a non-empty value."""

    code = "caption_empty_title"
```

`CaptionRequest` is the canonical conversation snapshot plus the current title;
it is what the caption prompt is built from. `CaptionResult` is the validated
title. `CaptionTitleError` carries the stable `code` that the Tool maps to a
failure outcome.

## Service contract

`CaptionService` (`XBotv2/caption/service.py`) owns the caption lifecycle for one
session runtime:

```python
class CaptionService:
    def __init__(
        self,
        *,
        events: Context,
        model: ModelPort,
        state: ThreadMetadataState,
        usage: UsagePort,
        session_id: str,
        config: CaptionConfig,
        is_subagent: bool = False,
    ) -> None: ...

    @property
    def title(self) -> str: ...
    async def caption_get(self) -> CaptionResult: ...
    async def caption_set(self, title: str) -> CaptionResult: ...
```

The constructor is keyword-only. `state` is the thread metadata state, not a
state namespace: the title is written through
`ThreadMetadataState.replace_title(...)`, so it travels with the thread
metadata rather than in a plugin-owned state namespace. This is why the plugin
injects `loop_state` (to reach `ctx.loop_state.metadata`) and `session` rather
than `state`.

`caption_get` reads the current title; `caption_set` normalizes the requested
title and raises `CaptionTitleError` when nothing non-empty remains.

## Tool contract (`caption/tools.py`)

```python
def build_caption_tool(owner: _CaptionToolOwner) -> Tool:
    async def caption(action: str, title: str = "") -> ToolOutcome:
        """Get or set the session's human-readable title.

        Sessions are labelled after the first message by default; use this to
        give the session a clear, meaningful name once the user states what
        the conversation is about, or to read the current name back.
        """
```

`action` is `"get"` or `"set"`. An unknown action returns
`failed_text("caption_bad_action", ...)`; a `CaptionTitleError` returns
`failed_text(exc.code, ...)`.

The owner is bound before registration as a typed `_CaptionToolOwner` Protocol,
so the Tool itself holds no `Context`.

## Extension notes

- The plugin is an observer, not a guard: do not short-circuit
  `context/before-build` from a caption-like observer, and do not register a
  second listener that assumes it runs first.
- Captioning issues its own model request. If you add a similar naming or
  classification feature, reuse this shape — an independent request whose
  output is written to thread metadata — instead of appending synthetic turns
  to the conversation.
- Do not persist the title in a plugin state namespace. Thread metadata is the
  owner, and the tool and the automatic path must agree on one source.
