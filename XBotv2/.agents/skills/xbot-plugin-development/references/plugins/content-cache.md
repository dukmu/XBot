# `content-cache`

Caches oversized current user messages as artifacts, replacing the
message body with a preview at the model request boundary. This frees
context tokens for the conversation while preserving full content
access.

- **Import/profile:** `content-cache`, Agent profile.
- **Source:** `XBotv2/content_cache/plugin.py`,
  `XBotv2/content_cache/content_cache.py`,
  `XBotv2/content_cache/config.py`.
- **Injects/provides:** `artifacts` → `content_cache`
  (`ContentCacheService`).
- **Subscribes to events:** `before/model-request` (bind cached message).

## Public data models

### `ContentCacheService` (`XBotv2/content_cache/plugin.py:14-43`)

```python
class ContentCacheService:
    """Create and reuse provider copies for oversized current user messages."""

    def __init__(
        self,
        artifacts: ArtifactStorePort,
        config: ContentCacheConfig,
    ) -> None:
        self._artifacts = artifacts
        self._config = config
        self._cached: dict[int, tuple[Message, Message]] = {}

    def bind_current_user_message(self, messages: list[Message]) -> list[Message]:
        """Return messages with the current user message bounded/replaced."""
```

### `ContentCacheHandler`

```python
class ContentCacheHandler:
    def __init__(self, service: ContentCacheService) -> None:
        self._service = service

    async def bind_model_request(self, event: EventContext) -> None:
        request = event.model_request
        if request is not None:
            request.messages = self._service.bind_current_user_message(
                request.messages
            )
```

### `ContentCacheConfig` / `CONFIG_SCHEMA`

```python
CONFIG_SCHEMA = S.object({
    "cache_threshold_chars": S.number().optional(),  # default 12000
    "preview_chars": S.number().optional(),           # default 8000
    "tail_chars": S.number().optional(),              # default 2000
})

@dataclass(frozen=True, slots=True)
class ContentCacheConfig:
    cache_threshold_chars: int = 12_000
    preview_chars: int = 8_000
    tail_chars: int = 2_000
```

### `cache_user_message` (`XBotv2/content_cache/content_cache.py`)

```python
def cache_user_message(
    message: Message,
    artifacts: ArtifactStorePort,
    *,
    cache_threshold_chars: int = 12_000,
    preview_chars: int = 8_000,
    tail_chars: int = 2_000,
) -> tuple[Message, ArtifactRef | None]:
    """Bounce oversized messages to artifact store.

    Returns (bounded_message, artifact_ref).
    If the message fits within threshold, returns (message, None).
    """
```

The bounded message replaces the original text with a preview:
```
[content truncated to N chars, full content cached in artifact]
```

### `ContentCacheComponent`

```python
class ContentCacheComponent:
    inject = ["artifacts"]
    name = "xbot.content_cache"
    Config = CONFIG_SCHEMA

    def apply(self, ctx: Any, config: Any = None) -> None:
        service = ContentCacheService(
            ctx.artifacts, parse_content_cache_config(config)
        )
        ctx.set("content_cache", service)
        ctx.on(Events.BEFORE_MODEL_REQUEST, ContentCacheHandler(service).bind_model_request)
```

## How it works

`bind_current_user_message()` finds the **current** (last) user
message by walking backwards from the end. If it exceeds the
threshold, it is cached to the artifact store and replaced with a
preview. The bounded message is stored in `self._cached[id(source)]`
to avoid re-caching on subsequent calls.

## Typical extension: read cached content

```python
from XBotv2.core.artifacts import ArtifactKind

class ContentCachePlugin:
    inject = ["content_cache"]

    def apply(self, ctx, config):
        # content_cache is ContentCacheService — read ctx.content_cache._cached
        # to see what was cached this turn
        ...
```

## Cross-references

- Depends on: `artifacts`, `agentloop` (subscribes to
  `BEFORE_MODEL_REQUEST`).
- Depended on by: the Agent loop (message bounding at request time).
- Pairs with: `persistence` (original message persists in history).

## Common pitfalls

- **Caching only the current message**: `bind_current_user_message()`
  only looks at the last user message (index -1). Earlier user
  messages are not bounded.
- **Re-caching on subsequent turns**: the `_cached` dict uses
  `id(source)` to prevent duplicate caching. If the message object
  is replaced, it will be re-cached.
- **Config validation**: `preview_chars` must not exceed
  `cache_threshold_chars`; `tail_chars` must not exceed
  `preview_chars`. Invalid configs raise `ValueError`.
- **Artifact storage is session-relative**: cached content lives
  in the thread's artifact store and is accessible via
  `ArtifactStorePort.open(ref)`.
