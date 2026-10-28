# Prompts

The `prompts` service requires `context_builder` and publishes `PROMPTS` / `PromptsPort` from `api/service.py`. It accepts no dedicated configuration and maintains an in-memory registry, with no persisted state, commands, tools, or event subscriptions. Static prompt components are registered only inside a plugin's `apply`, where the current fiber owns them; unload and failed activation remove them. Dynamic per-build contributions use `CONTEXT_COMPONENTS_BUILT` instead.

```python
from XBotv2.context_builder import InlinePromptComponent
from XBotv2.prompts.api.service import PROMPTS

# Inside an applying plugin with the prompts dependency:
ctx.require(PROMPTS).add(InlinePromptComponent(
    stage="context_suffix", source="example", text="Follow the project conventions."
))
```

Components enter the system prompt in stage order and can affect provider prefix caching. The service wraps the existing registry; it does not read files or expand variables.
