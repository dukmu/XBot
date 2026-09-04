# `server-routes-config`

Session-level policy management HTTP routes — get and patch permissions
and sandbox settings. Registered via `contribute_router()` as
`xbot.http.config`.

- **Import/profile:** `server-routes-config`, server profile.
- **Source:** `XBotv2/config/protocol.py`,
  `XBotv2/config/http/plugin.py`.
- **Injects/provides:** none (uses `contribute_router`).
- **Subscribes to events:** `http/route` (`REGISTER_ROUTE`).

## Routes (`build_router`)

```python
def build_router(*, sessions: SessionsPort) -> APIRouter:
```

### `GET /sessions/{session_id}/policy` → `SessionPolicyResponse`

```python
@router.get(
    "/sessions/{session_id}/policy",
    operation_id="get_session_policy",
)
async def get_session_policy(session_id: str) -> SessionPolicyResponse:
    snapshots = await sessions.dispatch_all(
        session_id, GET_POLICY, EmptyRequest()
    )
    return _policy_response(session_id, snapshots[0])
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
    snapshots = await sessions.dispatch_all(
        session_id,
        UPDATE_POLICY,
        PatchPolicy(
            permissions=dict(payload.permissions) or None,
            remove_permissions=tuple(payload.remove_permissions),
            sandbox=dict(payload.sandbox) or None,
            remove_sandbox=tuple(payload.remove_sandbox),
        ),
    )
    return _policy_response(session_id, snapshots[0])
```

Dispatches `UPDATE_POLICY` (`config/policy/update`) with the
`PatchPolicy` converted from `SessionPolicyPatch`.

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
    permissions: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    effective_permissions: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    sandbox: dict[str, Any] = Field(default_factory=dict)
    effective_sandbox: dict[str, Any] = Field(default_factory=dict)
```

## Cross-references

- Depends on: `server` (`contribute_router`), `sessions` (`SessionsPort`).
- Depended on by: HTTP policy clients, TUI policy views.
- Pairs with: `config` (`GET_POLICY`, `UPDATE_POLICY`, `PolicySnapshot`),
  `permissions`, `sandbox`.

## Common pitfalls

- **Cannot set and remove the same key**: `_validate_policy_patch`
  raises `ValueError` if `permissions` and `remove_permissions`
  share a key, or `sandbox` and `remove_sandbox` share a key.
- **`enabled` and `network` must be booleans**: other sandbox keys
  must be access modes (`allow`, `deny`, etc.). The validator
  checks `isinstance(value, bool)` for the two boolean keys.
- **Permission tool names are stripped**: `_validate_permission_names`
  strips whitespace from keys. `" shell "` becomes `"shell"`.
- **`dispatch_all` returns one snapshot per plugin**: since only
  the `config` plugin handles `GET_POLICY`/`UPDATE_POLICY`,
  `snapshots[0]` is always the result. Other plugins that
  subscribe to these operations would receive their own results.
