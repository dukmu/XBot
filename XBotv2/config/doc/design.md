# Configuration

The config plugin resolves plugin trees and session policy, expands runtime variables, and exposes session settings through `SETTINGS` (`api/contracts.py`). The shared `core.api.domain.UserContext` identity is used when constructing model context. Policy changes emit `POLICY_CHANGED` (`api/events.py`); HTTP wire models live in `api/protocol.py`.

It depends on loader tree types, permissions and sandbox policy models, runtime paths and variables. Resolution reads configuration and workspace files; policy updates write session overlays and emit an event. The service retains session paths and runtime options and resolves the tree when queried; session overlays are persisted on disk. `ConfigPluginConfig` defaults to an empty `instructions` string and a `UserContext` with `default-user`, `User`, `terminal` and `interactive`. Plugin trees combine built-in, global, workspace and session layers; runtime variables are expanded for the active thread when the tree is resolved. The runtime application and loader roots remain outside this migration.

Session policy updates through the policy operation notify each active thread in that session. Each thread
resolves the persisted overlay using its own variables and runtime overrides
before its permission and sandbox handlers apply the change. Newly opened
threads resolve the current overlay on startup. The generic plugin configuration
editor retains its separate new-session application boundary.

`RuntimeConfig` contains resolved instructions and tool selection for Agent composition. The Agent loop reads memory separately through `SETTINGS.memory()` when it constructs the engine.

The implementation owns routes, seed resources and persistence details. For example:

```python
from XBotv2.config.api.contracts import SETTINGS, PatchPolicy

user = ctx.require(SETTINGS).user_context()
await ctx.require(SETTINGS).update_policy(PatchPolicy(permissions={"shell": "ask"}))
```
