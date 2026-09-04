# `sandbox`

Session-level capability policy that gates every filesystem, process,
and network operation initiated through the Agent's core Tools. This is
**not** the OS-level bwrap that isolates the agent runtime itself.

- **Import/profile:** `sandbox`, Agent profile.
- **Source:** `XBotv2/sandbox/plugin.py`,
  `XBotv2/sandbox/policy.py`,
  `XBotv2/sandbox/bwrap.py`,
  `XBotv2/sandbox/commands.py`,
  `XBotv2/config/models.py` (`SandboxConfig`, `SandboxResourceConfig`).
- **Injects/provides:** thread paths, session, tools, data/workspace
  roots, variables, commands, settings → `sandbox` (`SandboxPolicy`).
- **Subscribes to events:** `policy/changed` (from config),
  `session/start`, `session/close`.
- **Server counterpart:** none — Agent-profile only.

## Public data models

### `SandboxConfig` (`XBotv2/config/models.py`)

```python
class SandboxResourceConfig(StrictModel):
    path: str
    access: Literal["allow", "readwrite", "readonly", "deny"] = "readonly"

class SandboxConfig(StrictModel):
    enabled: bool = True
    network: bool = True
    external_read:  Literal["allow", "readwrite", "readonly", "deny"] = "readonly"
    external_write: Literal["allow", "readwrite", "readonly", "deny"] = "deny"
    workspace_read:  Literal["allow", "readwrite", "readonly", "deny"] = "allow"
    workspace_write: Literal["allow", "readwrite", "readonly", "deny"] = "allow"
    resources: list[SandboxResourceConfig] = Field(default_factory=list)
```

`resources` is the **per-path override list**. Each entry mounts a
specific path with a specific `access` level — this is how a
plugin (or the session config) opens a single external directory
without flipping the global `external_write` flag.

### `SandboxPolicy` (`XBotv2/sandbox/policy.py`)

```python
PathAccess = Literal["allow", "readwrite", "readonly", "deny"]

@dataclass
class SandboxResourceRule:
    path: str
    access: PathAccess = "readonly"

class SandboxPolicy:
    def __init__(
        self,
        config: SandboxConfig | None = None,
        *,
        data_root: Path | str = "/tmp/xbotv2-data",
        workspace_root: Path | str = "/tmp/xbotv2-workspace",
        session_root: Path | str | None = None,
        enabled: bool = True,
        network: bool = True,
        external_read: str = "readonly",
        external_write: str = "deny",
        workspace_read: str = "allow",
        workspace_write: str = "allow",
        variables: RuntimeVariables | None = None,
    ) -> None: ...

    # Roots (resolved at construction)
    enabled: bool
    data_root: Path
    workspace_root: Path
    session_root: Path | None
    variables: RuntimeVariables

    # Axis policies (strings, not literals — see validation)
    external_read: str
    external_write: str
    workspace_read: str
    workspace_write: str

    # Properties
    @property
    def network(self) -> bool: ...
    @property
    def backend_available(self) -> bool: ...

    # Internal (fiber-private)
    _network: bool
    _rules: list[SandboxResourceRule]
    _backend: BubblewrapBackend
```

`network` is exposed as a property; the underlying `_network` is private
to prevent callers from mutating after construction. `variables` is the
`RuntimeVariables` for path expansion (`${VAR}` substitution in
resource paths).

### `PatchPolicy` (`XBotv2/config/models.py`)

```python
class ConfigOverlay(StrictModel):
    ...
    sandbox: SandboxConfig | None = None

class PatchPolicy(BaseModel):
    sandbox: dict[str, PathAccess] | None = None
    remove_sandbox: tuple[str, ...] | None = None
```

Used by `/sandbox set` and the config service to mutate one session's
policy without rewriting the entire config. Note: `PatchPolicy.sandbox`
only patches the **axis fields**, not `resources` — to add or change
resources, push a full `SandboxConfig` via `ConfigOverlay`.

## `SandboxPolicy` public methods

### Resource rules

```python
def add_rule(
    self, path: str, access: PathAccess
) -> SandboxResourceRule:
    """Prepend a per-path rule. Resolved against `variables` at mount time."""

def replace_config(self, config: SandboxConfig) -> None:
    """Replace policy state without invalidating runtime references.

    Constructs a new policy off-thread, then copies fields onto self,
    preserving the `_backend` instance and any fiber handles.
    """

def resolve_resource_path(self, path: str) -> str:
    """Expand ${VAR} (via `variables`) and resolve to an absolute path."""
```

### Path resolution

```python
def resolve_read_path(self, path: str) -> Path:
    """Absolute → as-is; 'session/...' → under session_root; else → workspace."""

def resolve_write_path(self, path: str) -> Path:
    """Same logic; applied to write-side paths in coretools."""

def resolve_filesystem_args(
    self, operation: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Resolve every path-shaped field for one filesystem op.

    Uses `XBotv2/core/filesystem/operations.PATH_ACCESS` to know which
    fields are paths and whether they're read or write.
    """
```

### Access checks (used by the guard and coretools)

```python
def check_filesystem_access(
    self, operation: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    """Returns one decision per non-allow path; empty list = pass."""

def check_tool_access(
    self, tool_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    """Maps `tool_name` → filesystem operation, then delegates."""

def make_guard(self) -> Any:
    """Return the monotonic ToolGuard for the standard pipeline.

    The guard is enforcement-only: it returns `None` to pass (including
    escalated shell — the permission layer owns that), or a
    `GuardDecision(action="deny")` when the policy rejects a path.
    The sandbox never asks; human approval is the permission layer's job.
    """
```

### Sandbox execution

```python
async def run_shell(
    self,
    command: str,
    *,
    shell: str | None = None,
    cwd: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """Spawn `$SHELL -lc <command>` inside bwrap with the policy mounts."""

async def filesystem(
    self, operation: str, args: dict[str, Any]
) -> str:
    """Run one filesystem op via the trusted worker at
    `XBotv2/core/filesystem/operations.py` inside bwrap."""
```

Both delegate to `BubblewrapBackend.run(...)` after assembling mounts
from `_mount_specs()` and (for `filesystem`) the per-call
`_filesystem_mount_specs(...)`.

### Mount assembly

```python
def _mount_specs(self) -> list[SandboxMountSpec]: ...
    # data_root       → readonly
    # workspace_root  → derived from workspace_read + workspace_write
    # session_root    → readonly (if set)
    # rules           → per-resource mount (readwrite or readonly)
    # worker script   → readonly (so bwrap can find it)

def _filesystem_mount_specs(
    self, operation: str, args: dict[str, Any]
) -> list[SandboxMountSpec]:
    """Add per-call parent-directory mounts for approved paths outside
    the workspace. Mutations mount a parent because atomic replace,
    rename, and delete operate on directory entries. These mounts exist
    only for the trusted filesystem worker."""
```

### `_path_decision`

Internal but observable through `check_filesystem_access`:

```python
# Pseudocode of resolution order
def _path_decision(self, target: Path, *, write: bool) -> str:
    # 1. If target under workspace, use workspace_{read,write}.
    # 2. If target under session, allow (controlled by session layer).
    # 3. If target under any resource rule with write access, allow.
    # 4. Else use external_{read,write}.
```

## Slash command — `/sandbox`

### `Command` registration

```python
Command(
    name="sandbox",
    description="Inspect or update the session sandbox",
    handler=guard_command(sandbox_command),
    usage="/sandbox [status|set <key> <value>|reset [key]]",
)
```

### Handler

```python
async def sandbox_command(raw_args: str) -> CommandResult: ...
```

`raw_args` is parsed by `split_command_args(raw_args)`:

| Subcommand | Args | Behavior |
|---|---|---|
| `status` / `list` | (none) | prints `settings.policy().effective_sandbox` |
| `set <key> <value>` | key + value | patches one axis via `PatchPolicy(sandbox={key: parsed})` |
| `reset [key]` | optional key | clears one axis, or all six default axes |

### Accepted values

```python
VALID_BOOL_KEYS = {"enabled", "network"}
VALID_PATH_KEYS = {"external_read", "external_write",
                   "workspace_read", "workspace_write"}
BOOLEAN_VALUES = {"true", "false", "yes", "no", "1", "0"}
PATH_VALUES = {"allow", "deny", "ask", "readonly", "readwrite"}
```

`_sandbox_value(key, value)` (private) parses one axis or raises
`ValueError`, surfaced as `CommandResult(status="error")`.

> **Limitation:** `/sandbox set` only patches the **axes**, not
> `resources`. To add per-path rules, push a full `SandboxConfig`
> via the config service or `ctx.sandbox.add_rule(path, access)` from
> plugin code.

## bwrap integration (`XBotv2/sandbox/bwrap.py`)

```python
class BubblewrapBackend:
    def __init__(self, workspace_root: Path, *, network: bool) -> None: ...

    async def run(
        self,
        argv: list[str],
        mounts: list[SandboxMountSpec],
        *,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
        stdin: str | None = None,
    ) -> str: ...

def backend_available() -> bool: ...
```

Per-mount flags:

```python
bind_flag = "--bind-try" if mount.access == "readwrite" else "--ro-bind-try"
```

The bwrap namespace is rebuilt on **every** call (cheap, but not free)
so policy updates take effect immediately.

## How coretools consult the policy

```python
# Pseudocode (XBotv2/coretools/filesystem.py, coretools/shell.py)
async def invoke(args, *, ctx):
    issues = ctx.sandbox.check_filesystem_access(tool_op, args)
    if issues:
        return ToolResult.failure("denied", "; ".join(
            f"{i['path']}: {i['decision']}" for i in issues
        ))
    ...
```

`/sandbox set` does **not** affect the OS-level bwrap that surrounds
the agent runtime itself; that is controlled by the runtime's
`sandbox_permissions` parameter on `shell` / `edit` calls.

## Typical extension: per-resource open without flipping `external_write`

```python
class NetworkTool:
    inject = ["sandbox"]

    def apply(self, ctx, config):
        # Open one external path read-only without enabling external_read globally.
        ctx.sandbox.add_rule("/var/run/docker.sock", "readonly")
```

For tests, construct a `SandboxPolicy(config, data_root=tmp,
workspace_root=tmp)` directly and inject it as a service; do not
parse YAML or read `xcore.yaml` in a Tool.

## On-disk artifacts

None directly. The bwrap runtime under `XBotv2/sandbox/bwrap.py`
builds `--ro-bind-try` / `--bind-try` flag lists from `_mount_specs()`
on each call.

## Cross-references

- Depends on: `commands` (registers `/sandbox`), `settings` (patches
  the policy), `RuntimeVariables` (path expansion).
- Depended on by: `coretools`, `browser`, `permissions`,
  `permission_request`, `skills`, every Tool that touches the
  filesystem.
- Pairs with: `permissions` — sandbox is about *paths*, permissions
  is about *tool identity*. Both gates must pass.

## Common pitfalls

- **`/sandbox set external_write readwrite` opens *all* external
  paths**: there is no per-path argument to the slash command. Use
  `SandboxConfig.resources` (via config overlay) or
  `ctx.sandbox.add_rule(path, access)` from plugin code for a
  single-path exception.
- **`/sandbox set` does not edit `resources`**: it only patches the
  five axis fields. To add or remove resource rules, use
  `add_rule(...)` / `replace_config(...)` from code.
- **Assuming per-session persistence**: `/sandbox set` mutates the
  session's `SandboxConfig` (in settings); it does not persist to
  the data-dir config file unless the operator commits via
  config.save.
- **Calling raw `os` / `shutil` from a Tool**: only `ctx.tools`
  registry entries pass through the guard pipeline; direct calls
  bypass policy entirely.
- **Mutating `SandboxPolicy._rules` directly**: rules have order
  semantics (newest wins in `_mount_specs`). Use `add_rule(path, access)`.
- **Forgetting `variables` in a custom `SandboxConfig.resources` path**:
  resource paths go through `variables.expand(path, source="sandbox
  resource path")`. Undeclared `${VAR}` raises; check
  `RuntimeVariables` ownership for the path.
