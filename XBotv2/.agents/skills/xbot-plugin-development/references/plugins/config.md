# `config`

Path-bound configuration reading and runtime policy management. Resolves one
generic `PluginTree` from the layered overlay documents, exposes the resolved
user context, and handles session-level policy patches (permissions + sandbox).

- **Import/profile:** `config`, Agent profile.
- **Source:** `XBotv2/config/plugin.py`,
  `XBotv2/config/service.py`,
  `XBotv2/config/contracts.py`,
  `XBotv2/config/policy.py`,
  `XBotv2/config/loader.py`,
  `XBotv2/config/events.py`.
- **Injects/provides:** `runtime_log`, `runtime_paths`, `session_launch`,
  `plugin_overrides`, `plugin_dirs`, `no_plugins` → `settings`
  (`ConfigService`).
- **Permission approval:** grants belong to the permissions runtime and are
  not persisted by this plugin. Human policy commands remain persisted.
- **Operations:** `GET_POLICY` (`config/policy/get`), `UPDATE_POLICY`
  (`config/policy/update`).

## Public data models

### `ConfigService` (`XBotv2/config/service.py`)

```python
class ConfigService(SettingsPort):
    """Path-bound configuration reader with a resolved user context."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        session_id: str,
        workspace_root: Path,
        events: ApplicationEventsPort,
        runtime_log: RuntimeLog,
        extra_plugins: list[dict[str, JsonValue]] | None = None,
        plugin_dirs: list[Path | str] | None = None,
        is_subagent: bool = False,
        no_plugins: bool = False,
    ) -> None: ...

    def user_context(self) -> UserContext: ...

    def load_plugin_tree(
        self, workspace: Path, session_id: str
    ) -> PluginTree: ...

    def memory(self) -> str: ...

    def policy(self) -> PolicySnapshot: ...

    async def update_policy(self, patch: PatchPolicy) -> PolicySnapshot: ...
```

There is no `user_context` constructor parameter and no cached `_user_context`
attribute. `user_context()` is a **method** that reads the live resolved
`config` entry from the plugin tree on each call, so a session-scope overlay
write is reflected immediately instead of returning a stale
construction-time capture. It raises `RuntimeError` when the resolved tree has
no enabled `config` entry.

`policy()` returns the effective JSON objects from the resolved permission and
sandbox entries; `update_policy()` emits `POLICY_CHANGED` after persisting.
The config plugin does not redeclare or validate those plugin models.

### `UserContext` (`XBotv2/config/contracts.py`)

```python
class UserContext(StrictModel):
    user_id: str = "default-user"
    user_name: str = "User"
    platform: str = "terminal"
    session_type: str = "interactive"
```

Loaded from the plugin tree's `user` block, not a separate file.

### Generic tree boundary

```python
tree = ctx.settings.load_plugin_tree(workspace, session_id)
for entry in tree.entries:
    # entry.id/name/disabled/config are the only generic fields here.
    ...
```

The consumer that owns a declaration validates it with that plugin's model:

```python
from XBotv2.llm import LlmConfig

llm_entry = next(item for item in tree.entries if item.id == "llm")
llm = LlmConfig.model_validate(llm_entry.config)
```

Do not add a second aggregate model to `config` when adding a plugin.

### `PolicySnapshot` / `PatchPolicy` (`XBotv2/config/contracts.py`)

```python
@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    policy: dict[str, JsonValue]
    effective_permissions: dict[str, JsonValue]
    effective_sandbox: dict[str, JsonValue]

@dataclass(frozen=True, slots=True)
class PatchPolicy:
    permissions: dict[str, str] | None = None
    remove_permissions: tuple[str, ...] = ()
    sandbox: dict[str, JsonValue] | None = None
    remove_sandbox: tuple[str, ...] = ()

GET_POLICY = Operation("config/policy/get", EmptyRequest, PolicySnapshot)
UPDATE_POLICY = Operation(
    "config/policy/update", PatchPolicy, PolicySnapshot, exclusive=True,
)
```

### `PolicyChanged` event (`XBotv2/config/events.py`)

```python
@dataclass(frozen=True, slots=True)
class PolicyChanged:
    policy: dict[str, JsonValue]
    permission_policies: tuple[PermissionPolicy, ...]
    effective_sandbox: dict[str, JsonValue]

POLICY_CHANGED = "config/policy-changed"
```

`PolicyChanged` does **not** carry `effective_permissions`. The merged policy is
published as typed `permission_policies` — a tuple of the permissions plugin's
own `PermissionPolicy` objects — because a policy change must be consumable
without re-parsing raw JSON. Only the sandbox side stays a resolved JSON object,
in `effective_sandbox`.

The permission and sandbox models are owned by their respective plugins. The
config plugin only projects their resolved values; it does not redeclare or
validate those fields.

## How `apply()` works

```python
def apply(self, ctx: Context, config: ConfigPluginConfig) -> None:
    settings = ConfigService(
        ctx.runtime_paths,
        session_id=ctx.session_launch.session_id,
        workspace_root=ctx.session_launch.workspace_root,
        events=ctx,
        runtime_log=ctx.runtime_log,
        extra_plugins=ctx.plugin_overrides,
        plugin_dirs=ctx.plugin_dirs,
        is_subagent=ctx.session_launch.is_subagent,
        no_plugins=ctx.no_plugins,
    )
    ctx.set("settings", settings)
    operations = ConfigOperations(settings)
    ctx.on(GET_POLICY.name, operations.get_policy)
    ctx.on(UPDATE_POLICY.name, settings.update_policy)
```

The plugin exposes the generic tree and policy operations; it does not expose
an aggregate of other plugins' configuration.

## On-disk artifacts and layer order

The plugin tree is resolved once at startup. All declarations use the same
overlay grammar, including sandbox and permissions:

```text
XBotv2/xcore.yaml
  → <data-dir>/config/plugins.yaml
  → <workspace>/.xbot/plugins.yaml
  → <data-dir>/sessions/<session_id>/config.yaml
  → in-memory launch overrides
```

`config/policy.py` writes the session layer at:

```text
<data_dir>/sessions/<session_id>/
├── config.yaml              # per-session config snapshot
└── threads/<thread_id>/...
```

`config.yaml` is a plugin overlay document, for example:

```yaml
plugins:
  - id: permissions
    config:
      ask: [{tool: "shell"}]
  - id: sandbox
    config:
      network: false
```

`load_plugin_tree()` returns that same resolved tree; it does not read a
second aggregate `config.yaml` format. A launch override is an in-memory
`PluginOverlay` patch and is never persisted automatically.

## Typical extension: read policy

```python
from XBotv2.config import PolicySnapshot

class PolicyAwareTool:
    inject = ["settings", "tools"]

    def apply(self, ctx, config):
        snap = ctx.settings.policy()
        effective = snap.effective_permissions
        # effective is a dict[str, JsonValue] — policy snapshot only
        ...
```

## Cross-references

- Depends on: `runtime_log`, `runtime_paths`, `session_launch`,
  `plugin_overrides`, `plugin_dirs`, `no_plugins`.
- Depended on by: `sandbox` (reads its own sandbox entry through `settings`),
  `permissions` (reads `ctx.settings.permission_policies()`), and `agents`
  (passes `settings` into `AgentsService`).
- Not depended on by `coretools` or `skills`: neither injects `settings`.
  `coretools` reads its configuration from its own tree entry, and `skills`
  takes no configuration at all.
- Pairs with: `llm` (provider config lives in the `llm` tree entry, not here),
  `sandbox`, `commands` (`/sandbox`, `/permissions`).

## Common pitfalls

- **Adding plugin fields to a runtime aggregate**: configuration consumers
  should read their own tree entry and validate it with their declared
  Pydantic model. `ConfigService` is not a plugin registry of duplicated
  schemas.
- **Persisting `PERMISSION_DECIDED` grants** changes a runtime approval into
  durable policy. Do not do this; persisted policy requires a human settings
  operation.
- **Mutating `ConfigService._user_context`**: it is a property on
  initialization; do not set it after the service is created.
- **Reading `ctx.settings.policy()` during `apply()` before the
  `config` plugin has mounted**: in tests, construct
  `ConfigService(paths, session_id=sid, ...)` directly.
