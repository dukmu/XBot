# `permissions`

Policy enforcement for every registered Tool. The plugin walks the rule
list per call, decides `allow` / `deny` / `ask`, persists grants, and
hands off interactive decisions to `permission_request`. Never bypass
the guard pipeline — register Tools normally and let policy decide.

- **Import/profile:** `permissions`, Agent profile.
- **Source:** `XBotv2/permissions/plugin.py`,
  `XBotv2/permissions/system.py`, `XBotv2/permissions/services.py`,
  `XBotv2/permissions/guard.py`, `XBotv2/permissions/events.py`,
  `XBotv2/permissions/rules.py`,
  `XBotv2/permissions/tools.py`.
- **Injects/provides:** session/launch, parent permissions, tools,
  approval, variables, commands, settings → `permissions`
  (`PermissionsService`).
- **Emits events:** `permissions/decided` (`PermissionDecided`),
  `permission/request` (`PermissionRequested`) — see
  [../events-catalog.md](../events-catalog.md).

## Public data models

### `PermissionDecision`

```python
PermissionDecision = Literal["allow", "ask", "deny"]
```

Returned by `PermissionSystem.check`.

### `PermissionRule` (`permissions/system.py:62-68`)

```python
@dataclass
class PermissionRule:
    tool_pattern: str = ".*"            # regex against Tool.name
    param_patterns: dict[str, str] = field(default_factory=dict)
    paths: str | None = None            # resolved path scope
    decision: PermissionDecision = "ask"
```

Serialized form (`permissions/rules.py`):

```python
{"tool": "<regex>", "params": {...}, "paths": "<resolved>"}
```

### `PermissionsPort` — consumer Protocol

```python
class PermissionsPort(Protocol):
    def check(
        self,
        tool_name: str,
        args: dict[str, object] | None = None,
    ) -> str: ...

    def explicit_allow(
        self,
        tool_name: str,
        args: dict[str, object] | None = None,
        *,
        constrain_param: str | None = None,
    ) -> bool: ...

    def check_tool_call(self, tool_call: ToolCall) -> tuple[str, str]: ...

    def grant_once(
        self, tool_name: str, param_patterns: dict[str, str]
    ) -> None: ...
```

Declare `inject = ["permissions"]` and resolve to `ctx.permissions`.
Do **not** import `PermissionsService` directly.

### `PermissionDecided` / `PermissionRequested` (`permissions/events.py`)

```python
@dataclass(frozen=True, slots=True)
class PermissionDecided:
    decision: Literal["allow", "deny"]   # only terminal decisions emitted
    scope: str                          # "session" or one-shot id
    rule: dict[str, JsonValue]          # matched rule payload

@dataclass(frozen=True, slots=True)
class PermissionRequested:
    tool_call: ToolCall
    client_event: ClientEvent

PERMISSION_DECIDED = "permissions/decided"
PERMISSION_REQUESTED = "permission/request"
```

### `PermissionConfig` (`XBotv2/config/models.py`)

```python
class PermissionRuleConfig(StrictModel):
    tool: str = ".*"
    params: dict[str, str] = Field(default_factory=dict)
    paths: str | None = None

class PermissionConfig(StrictModel):
    deny: list[PermissionRuleConfig] = Field(default_factory=list)
    allow: list[PermissionRuleConfig] = Field(default_factory=list)
    ask: list[PermissionRuleConfig] = Field(default_factory=list)
```

## `PermissionsService` (`permissions/plugin.py:33-110`)

```python
class PermissionsService:
    def configure_agent(self, agent: Any) -> None: ...
    def replace_rules(self, config: object) -> None: ...
    def add_rule(self, decision: str, rule: dict[str, Any]) -> None: ...

    def check(
        self, tool_name: str, args: dict[str, Any] | None = None
    ) -> str: ...                        # decision literal

    def explicit_allow(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        constrain_param: str | None = None,
    ) -> bool: ...

    def check_tool_call(
        self, tool_call: ToolCall
    ) -> tuple[str, str]: ...             # (decision, reason)

    def grant_once(
        self, tool_name: str, param_patterns: dict[str, str]
    ) -> None: ...
```

The service rebuilds its internal `PermissionSystem` on rule changes;
observers must subscribe to `PERMISSION_DECIDED` to react to *terminal*
decisions (no event fires for `ask` mid-flight — see
`permission_request`).

## `PermissionGuard` (`permissions/guard.py`)

Registered against `ctx.tools.guard(...)` so every Tool dispatch runs:

```python
class PermissionGuard:
    def __init__(
        self,
        service: PermissionsService,
        *,
        policy: PermissionSystem,
    ) -> None: ...

    def allow(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        tool_call: ToolCall | None = None,
    ) -> GuardDecision | None: ...
```

Returning a `GuardDecision(action="deny", reason="...",
source="permissions")` short-circuits the dispatch and emits
`tool/denied`. The default delegates to
`PermissionsService.check_tool_call`.

## `PermissionSystem` (`permissions/system.py:103-400`)

```python
class PermissionSystem:
    """Tri-state permission system. deny > allow > ask > default."""

    def __init__(
        self,
        config: Any | None = None,
        *,
        default_decision: PermissionDecision = "ask",
        variables: RuntimeVariables | None = None,
        parent: PermissionsPort | None = None,
    ) -> None:
        self.default_decision = default_decision
        self.variables = variables or RuntimeVariables()
        self.parent = parent
        self._rules: dict[PermissionDecision, list[PermissionRule]] = {
            decision: [] for decision in ("deny", "allow", "ask")
        }
        self._once_grants: list[PermissionRule] = []
        if config is not None:
            self._load_config(config)

    def add_rule(
        self, decision: PermissionDecision, rule_data: dict[str, Any]
    ) -> None:
        """Add one live permission rule to the in-memory policy."""

    def replace_rules(self, config: Any | None) -> None:
        """Replace configured rules without invalidating shared references."""

    def grant_once(
        self, tool_name: str, param_patterns: dict[str, str]
    ) -> None:
        """Allow the next call matching one exact-name parameter rule."""

    def check(
        self, tool_name: str, args: dict[str, Any] | None = None
    ) -> PermissionDecision:
        """deny > allow > ask > default. Delegates to parent if set."""

    def explicit_allow(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        constrain_param: str | None = None,
    ) -> bool:
        """Promote an ask to allow by appending a one-shot rule."""

    def check_tool_call(
        self, tool_call: Any
    ) -> tuple[PermissionDecision, str]:
        """Per-call decision plus a human-readable reason."""
```

`_load_config` accepts a `BaseModel` (`PermissionConfig`) or a plain
`Mapping`. Rule patterns are validated as Python regexes at parse
time; `paths` go through `variables.expand_regex(paths, ...)` so
`${VAR}` substitution works in path patterns.

`permissions/tools.py` exports the `resolve_operation(tool_name, args)`
helper that maps a Tool call to its underlying filesystem operation.

## On-disk artifacts

None directly. Rules live in `Settings` configuration (config service
owns persistence). One-time grants are runtime facts only — never
persisted to disk.

## Typical extension: a permission-aware Tool

```python
from XBotv2.core import Tool, ToolResult

class NetworkTool:
    inject = ["permissions", "session"]

    def apply(self, ctx, config):
        async def ping(host: str) -> ToolResult:
            decision = ctx.permissions.check("ping", {"host": host})
            if decision == "deny":
                return ToolResult.failure("denied", "policy denies ping")
            return ToolResult.success(await _do_ping(host))
        ctx.tools.register(Tool.from_function(ping, name="ping"))
```

To require an interactive prompt for a particular tool, set the rule
decision to `"ask"`; the guard hands off to `permission_request`.

## Cross-references

- Depends on: `tools` (registers the guard), `approval` (interactive
  decisions via `permission_request`), `commands` (`/permissions`),
  `settings` (rule config).
- Depended on by: `coretools`, every Tool-registered plugin.
- Pairs with: [permission-request.md](permission-request.md)
  (interactive approval flow), [sandbox.md](sandbox.md) (path
  capability, orthogonal axis).

## Common pitfalls

- **Bypassing the guard for "trusted" tools**: every Tool passes
  through `ctx.tools.guard(...)`. A Tool that calls its handler
  directly from a coroutine skips sandbox, permissions, and event
  observers.
- **Importing `PermissionsService` instead of the `Protocol`**:
  `PermissionsPort` is the consumer contract; concrete class is
  implementation detail.
- **Persisting one-time grants to disk**: `grant_once` is intentionally
  runtime-only. Persisting it would create a parallel rule file.
- **Treating `ask` as a terminal decision**: only `allow` / `deny` emit
  `PERMISSION_DECIDED`. Listen for `PERMISSION_REQUESTED` if you
  need to react to mid-flight prompts.
- **Reading `ctx.permissions.check(...)` from inside a Tool handler**:
  the guard has already run before dispatch. Use `PermissionsPort`
  to decide *whether to call* a tool at the orchestration layer; do
  not re-check inside.
- **Mutating rules via `add_rule` from a request handler**: prefer
  `replace_rules` from a config update; runtime `add_rule` is for
  short-lived, session-scoped grants.
