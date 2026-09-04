# `llm-commands`

Slash commands for runtime provider, model, and reasoning effort selection.
These are **runtime commands**, not provider config changes — they
switch the active `ModelPort` binding in the current session.

- **Import/profile:** `llm-commands`, Agent profile.
- **Source:** `XBotv2/llm/commands.py`,
  `XBotv2/llm/runtime_commands/plugin.py` (registration).
- **Injects/provides:** `agent_runtime` (`AgentRuntimePort`), `llm`
  (`LlmCatalogPort`).
- **Operations:** none — pure `Command` registration.
- **Effects:** `("thread",)` on provider/model/effort switches.

## Public data models

### `build_llm_commands` (`XBotv2/llm/commands.py:18-120`)

```python
def build_llm_commands(
    runtime: AgentRuntimePort,
    llm: LlmCatalogPort,
) -> tuple[Command, ...]: ...
```

Returns a tuple of exactly 3 `Command` objects.

### `Command` registration (runtime_commands/plugin.py)

The commands are registered in `build_llm_runtime_commands()` which
calls `build_llm_commands()`:

```python
def build_llm_runtime_commands(
    runtime: AgentRuntimePort,
    llm: LlmCatalogPort,
) -> tuple[Command, ...]:
    return build_llm_commands(runtime, llm)
```

## Slash commands

### `/provider`

```
/provider [status|list|use <name>]
```

| Subcommand | Args | Behavior |
|---|---|---|
| `status` | (none) | `"Provider: {selected.provider} ({selected.model})"` |
| `list` | (none) | Lists all provider names; marks current with `(current)` |
| `use <name>` | provider name | Calls `runtime.select_provider(name, model=None)` |

### `/model`

```
/model [status|list|use [<provider>] <model>]
```

| Subcommand | Args | Behavior |
|---|---|---|
| `status` | (none) | `"Model: {selected.provider} ({selected.model})"` |
| `list` | (none) | Per-provider model list; `*` marks current |
| `use [<provider>] <model>` | 1 or 2 args | Switches provider+model; if one arg, keeps current provider |

### `/effort`

```
/effect [<level>]
```

| Subcommand | Args | Behavior |
|---|---|---|
| (none) | (none) | Shows `Effort: {model_mode or 'default'} (tiers)` or `"no effort tiers"` |
| `<level>` | effort tier | Calls `runtime.select_effort(tier)` |

## `AgentRuntimePort` usage

Commands delegate to `AgentRuntimePort`:

```python
selected = runtime.current_selection()    # -> SessionSelection
await runtime.select_provider(name, model=model)  # -> dict[str, str]
await runtime.select_effort(tier)         # -> dict[str, str]
```

`SessionSelection` (from `agents/services.py`) carries `provider`,
`model`, and `model_mode` attributes.

## Typical extension: add a custom LLM command

```python
from XBotv2.commands import Command, command_usage, guard_command

def my_model_command(raw_args: str) -> CommandResult:
    return CommandResult("My custom model info")

class MyLlmCommandsPlugin:
    name = "my-llm-commands"
    inject = ["agent_runtime", "llm", "commands"]

    def apply(self, ctx, config):
        commands = build_llm_commands(ctx.agent_runtime, ctx.llm)
        extra = (Command(
            name="my-model",
            description="Custom model info",
            handler=guard_command(my_model_command),
        ),)
        for cmd in commands + extra:
            ctx.commands.register(cmd)
```

## Cross-references

- Depends on: `agent_runtime` (`AgentRuntimePort`), `llm` (`LlmCatalogPort`),
  `commands`.
- Depended on by: end-user (slash commands).
- Pairs with: `llm` (provider catalog + model config), `agent-runtime`
  (runtime selection binding).

## Common pitfalls

- **Using `/model use` to switch to an unknown model**: `select_provider`
  validates against the `ModelPort` catalog; unknown model names raise.
- **Assuming `/effort` changes persist to config.yaml**: it only
  updates the runtime binding; the session overlay is not modified.
- **Mixing up `model_mode` and `reasoning_effort`**: `model_mode` is
  the *combined* selection (`reasoning_effort` or `thinking`), while
  `reasoning_effort` is the specific effort tier.
