# `server-routes-config`

Session-level policy management HTTP routes — get and patch permissions
and sandbox settings. Registered via `contribute_router()` as
`xbot.config.http`.

- **Import/profile:** `server-routes-config`, server profile.
- **Source:** `XBotv2/config/protocol.py`,
  `XBotv2/config/plugin.py` (`mount_http`).
- **Injects/provides:** none (uses `contribute_router`).
- **Registration:** the owning root plugin waits for `server` and `sessions`,
  then contributes the router as one fiber-owned server effect.

## Routes (`build_router`)

```python
def build_router(*, sessions: SessionsPort, settings: SettingsPort) -> APIRouter:
```

The session-scoped policy routes resolve the session's **main thread**
explicitly before dispatching. They do not use `dispatch_all`: taking the first
of several active threads silently depended on thread-id ordering, so a
subagent thread could win. A session with no active main thread raises
`OperationError("thread_not_active", ...)`.

```python
async def _main_thread_id(session_id: str) -> str:
    threads = await sessions.list_threads(session_id)
    main = next(
        (thread.thread_id for thread in threads if thread.kind == "main"), ""
    )
    if not main:
        raise OperationError(
            "thread_not_active",
            "Session policy requires an active main thread.",
        )
    return main
```

| Method | Path | `operation_id` | Response |
|---|---|---|---|
| `GET` | `/sessions/{session_id}/policy` | `get_session_policy` | `SessionPolicyResponse` |
| `PATCH` | `/sessions/{session_id}/policy` | `update_session_policy` | `SessionPolicyResponse` |
| `GET` | `/sessions/{session_id}/threads/{thread_id}/plugin-config` | `list_plugin_config` | `PluginConfigCatalog` |
| `PATCH` | `/sessions/{session_id}/threads/{thread_id}/plugin-config/{plugin_id}` | `update_plugin_config` | `PluginConfigCatalog` |

### `GET /sessions/{session_id}/policy` → `SessionPolicyResponse`

```python
@router.get(
    "/sessions/{session_id}/policy",
    operation_id="get_session_policy",
)
async def get_session_policy(session_id: str) -> SessionPolicyResponse:
    thread_id = await _main_thread_id(session_id)
    snapshot = await sessions.dispatch(
        session_id, thread_id, GET_POLICY, EmptyRequest()
    )
    return _policy_response(session_id, snapshot)
```

Dispatches `GET_POLICY` (`config/policy/get`) and converts the
`PolicySnapshot` to `SessionPolicyResponse`.

### `PATCH /sessions/{session_id}/policy` → `SessionPolicyResponse`

```python
@router.patch(
    "/sessions/{session_id}/policy",
    operation_id="update_session_policy",
)
async def update_session_policy(
    session_id: str,
    payload: SessionPolicyPatch,
) -> SessionPolicyResponse:
    thread_id = await _main_thread_id(session_id)
    snapshot = await sessions.dispatch(
        session_id,
        thread_id,
        UPDATE_POLICY,
        PatchPolicy(
            permissions=dict(payload.permissions) or None,
            remove_permissions=tuple(payload.remove_permissions),
            sandbox=dict(payload.sandbox) or None,
            remove_sandbox=tuple(payload.remove_sandbox),
        ),
    )
    return _policy_response(session_id, snapshot)
```

Dispatches `UPDATE_POLICY` (`config/policy/update`) with the `PatchPolicy`
converted from `SessionPolicyPatch`.

### Plugin-config routes

The two `plugin-config` routes are the read/write surface for per-plugin tree
entries. Both take a `scope: PluginConfigScope = Query(default="workspace")`
and resolve the workspace through the thread summary, then delegate to
`settings.plugin_config_catalog(...)` / `settings.update_plugin_config(...)`.
A `ValueError` from the settings service is re-raised as
`HttpServerError` (`XBotv2.protocol.http_util`) — `invalid_plugin_config` with
`status=400` on read/patch validation, and `plugin_config_conflict` with
`status=409` on a conflicting patch.

## Wire models

```python
PermissionDecision = Literal["allow", "deny", "ask"]
SandboxAccess = Literal["allow", "deny", "readonly", "readwrite"]
SandboxKey = Literal[
    "enabled", "network",
    "external_read", "external_write",
    "workspace_read", "workspace_write",
]
SandboxValue = StrictBool | SandboxAccess

class SessionPolicyPatch(WireModel):
    permissions: dict[str, PermissionDecision] = Field(default_factory=dict)
    remove_permissions: list[str] = Field(default_factory=list)
    sandbox: dict[SandboxKey, SandboxValue] = Field(default_factory=dict)
    remove_sandbox: list[SandboxKey] = Field(default_factory=list)

    # Validators:
    # - permission tool names must be non-empty
    # - removed permission names must be non-empty
    # - no overlap between permissions and remove_permissions
    # - no overlap between sandbox and remove_sandbox
    # - enabled/network must be bool; others must be access mode

class SessionPolicyResponse(WireModel):
    session_id: str = Field(min_length=1)
    permissions: PermissionPolicy = Field(default_factory=PermissionPolicy)
    effective_permissions: PermissionPolicy = Field(default_factory=PermissionPolicy)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    effective_sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
```

The response carries the **typed** `PermissionPolicy` and `SandboxConfig`
models, not raw `dict[...]` blobs. A client that generates its own schema from
the OpenAPI document therefore sees the real policy shapes; do not "simplify"
them back to dictionaries.

## Cross-references

- Depends on: `server` (`contribute_router`), `sessions` (`SessionsPort`),
  `settings` (`SettingsPort`, for the plugin-config routes).
- Depended on by: HTTP policy clients, TUI policy views.
- Pairs with: `config` (`GET_POLICY`, `UPDATE_POLICY`, `PolicySnapshot`),
  `permissions`, `sandbox`.

## Pitfalls

- **Cannot set and remove the same key**: `_validate_policy_patch`
  raises `ValueError` if `permissions` and `remove_permissions`
  share a key, or `sandbox` and `remove_sandbox` share a key.
- **`enabled` and `network` must be booleans**: other sandbox keys
  must be access modes (`allow`, `deny`, etc.). The validator
  checks `isinstance(value, bool)` for the two boolean keys.
- **Permission tool names are stripped**: `_validate_permission_names`
  strips whitespace from keys. `" shell "` becomes `"shell"`.
- **Policy is addressed through the main thread**: a session with no active
  main thread cannot read or patch policy. Do not reintroduce `dispatch_all`
  with a `[0]` index.
