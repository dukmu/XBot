# `config`

Path-bound configuration reading and runtime policy management. Loads
`RuntimeConfig` from disk on startup, exposes the resolved user context,
and handles session-level policy patches (permissions + sandbox).

- **Import/profile:** `config`, Agent profile.
- **Source:** `XBotv2/config/plugin.py`,
  `XBotv2/config/service.py`,
  `XBotv2/config/contracts.py`,
  `XBotv2/config/models.py`,
  `XBotv2/config/policy.py`,
  `XBotv2/config/loader.py`,
  `XBotv2/config/events.py`.
- **Injects/provides:** `runtime_log`, `runtime_paths`,
  `session_launch` → `settings` (`ConfigService`).
- **Subscribes to events:** `permissions/decided` (`PermissionDecided`) —
  persists the decision to disk via `PermissionRulePersister`.
- **Operations:** `GET_POLICY`, `UPDATE_POLICY`.

## Public data models

### `ConfigService` (`XBotv2/config/service.py:27-67`)

```python
class ConfigService:
    """Path-bound configuration reader with a resolved user context."""

    def __init__(
        self,
        paths: Any,
        *,
        session_id: str,
        workspace_root: Any,
        events: Any,
        runtime_log: RuntimeLog,
        user_context: UserContext | None = None,
    ) -> None:
        self.paths = paths
        self.session_id = session_id
        self.workspace_root = workspace_root
        self.events = events
        self._user_context = user_context or UserContext()

    def user_context(self) -> UserContext: ...

    def load_runtime_config(
        self, workspace: Any, session_id: str
    ) -> RuntimeConfig: ...

    def policy(self) -> PolicySnapshot: ...

    async def update_policy(self, patch: PatchPolicy) -> PolicySnapshot: ...
```

`policy()` calls `load_runtime_config` internally to resolve permissions
and sandbox; `update_policy()` emits `POLICY_CHANGED` after persisting.

### `UserContext` (`XBotv2/config/models.py:14-19`)

```python
class UserContext(StrictModel):
    user_id: str = "default-user"
    user_name: str = "User"
    platform: str = "terminal"
    session_type: str = "interactive"
```

Loaded from the plugin tree's `user` block, not a separate file.

### `RuntimeConfig` (`XBotv2/config/models.py:74-92`)

```python
class RuntimeConfig(StrictModel):
    provider: str = "default"
    max_concurrent_subagents: int = Field(default=4, ge=1)
    tool_results: ToolResultConfig = Field(default_factory=ToolResultConfig)
    tools: list[str] | None = None
    workspace_tools: list[WorkspaceToolConfig] = Field(default_factory=list)
    hooks: list[HookConfig] = Field(default_factory=list)
    plugins: dict[str, PluginConfig] = Field(default_factory=dict)
    plugin_paths: list[str] = Field(default_factory=list)
    permissions: PermissionConfig = Field(default_factory=lambda: PermissionConfig(
        ask=[PermissionRuleConfig(tool=".*")]
    ))
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    instructions: str = ""
    memory: str = ""
    agent_name: str = "XBotv2"
    agent_role: str = ""
    agent_instructions: str = ""
    max_context_tokens: int = 32_000
    max_output_tokens: int | None = None

    @property
    def plugin_configs(self) -> dict[str, dict[str, Any]]:
        return {
            name: entry.config
            for name, entry in self.plugins.items()
            if entry.enabled
        }
```

### `PolicySnapshot` / `PatchPolicy` (`XBotv2/config/contracts.py`)

```python
@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    policy: dict[str, object]
    effective_permissions: dict[str, object]
    effective_sandbox: dict[str, object]

@dataclass(frozen=True, slots=True)
class PatchPolicy:
    permissions: dict[str, str] | None = None
    remove_permissions: tuple[str, ...] = ()
    sandbox: dict[str, object] | None = None
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
    policy: dict[str, object]
    config: RuntimeConfig

POLICY_CHANGED = "config/policy/changed"
```

### `StrictModel`

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
```

### `PermissionConfig` / `SandboxConfig` / `PluginConfig`

```python
class PermissionConfig(StrictModel):
    deny: list[PermissionRuleConfig] = Field(default_factory=list)
    allow: list[PermissionRuleConfig] = Field(default_factory=list)
    ask: list[PermissionRuleConfig] = Field(default_factory=list)

class SandboxConfig(StrictModel):
    enabled: bool = True
    network: bool = True
    external_read: Literal["allow", "readwrite", "readonly", "deny"] = "readonly"
    external_write: Literal["allow", "readwrite", "readonly", "deny"] = "deny"
    workspace_read: Literal["allow", "readwrite", "readonly", "deny"] = "allow"
    workspace_write: Literal["allow", "readwrite", "readonly", "deny"] = "allow"
    resources: list[SandboxResourceConfig] = Field(default_factory=list)

class PluginConfig(StrictModel):
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
```

## How `apply()` works (`plugin.py:18-50`)

```python
def apply(self, ctx: Context, config: object | None = None) -> None:
    config = config or {}
    user = UserContext.model_validate(config.get("user") or {})
    settings = ConfigService(
        ctx.runtime_paths,
        session_id=ctx.session_launch.session_id,
        workspace_root=ctx.session_launch.workspace_root,
        events=ctx,
        user_context=user,
        runtime_log=ctx.runtime_log,
    )
    ctx.set("settings", settings)
    operations = ConfigOperations(settings)
    ctx.on(GET_POLICY.name, operations.get_policy)
    ctx.on(UPDATE_POLICY.name, settings.update_policy)
    persister = PermissionRulePersister(
        paths=ctx.runtime_paths,
        session_id=ctx.session_launch.session_id,
        runtime_log=ctx.runtime_log,
    )
    ctx.on(PERMISSION_DECIDED, persister.persist)
```

The plugin never exposes `RuntimeConfig` itself — only `policy()`
(sandbox+permissions) and `update_policy()`.

## On-disk artifacts

`config/policy.py` handles persistence at these paths:

```text
<data_dir>/sessions/<session_id>/
├── config.yaml              # per-session config snapshot
└── threads/<thread_id>/...
```

`patch_session_policy()` writes to `config.yaml` (session-level overlay);
`load_runtime_config()` reads the full resolution (global + session +
workspace + agent-local overlays).

## Typical extension: read policy

```python
from XBotv2.config import PolicySnapshot

class PolicyAwareTool:
    inject = ["settings", "tools"]

    def apply(self, ctx, config):
        snap = ctx.settings.policy()
        effective = snap.effective_permissions
        # effective is a dict[str, Any] — policy snapshot only
        ...
```

## Cross-references

- Depends on: `runtime_log`, `runtime_paths`, `session_launch`,
  `permissions` (subscribes to `PERMISSION_DECIDED`).
- Depended on by: `sandbox` (reads `SandboxConfig`), `permissions`
  (reads `PermissionConfig`), `coretools` (reads `ToolResultConfig`),
  `skills` (reads `PluginConfig`).
- Pairs with: `llm` (provider config lives in `llm` tree, not here),
  `sandbox`, `commands` (`/sandbox`, `/permissions`).

## Common pitfalls

- **Importing `RuntimeConfig` from `models.py` and using it for runtime
  checks**: `ConfigService.policy()` returns `PolicySnapshot` (only
  sandbox + permissions). If you need the full `RuntimeConfig`, call
  `load_runtime_config(workspace, session_id)` directly.
- **Persisting permissions from `PERMISSION_DECIDED` manually**: the
  `config` plugin owns this via `PermissionRulePersister`. Your plugin
  should not duplicate that logic.
- **Mutating `ConfigService._user_context`**: it is a property on
  initialization; do not set it after the service is created.
- **Reading `ctx.settings.policy()` during `apply()` before the
  `config` plugin has mounted**: in tests, construct
  `ConfigService(paths, session_id=sid, ...)` directly.
