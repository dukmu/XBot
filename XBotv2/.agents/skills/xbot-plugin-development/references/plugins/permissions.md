# Permissions: Tool parameter authorization

Tree id/import: `permissions`, Agent profile.
Source: `XBotv2/permissions/plugin.py`, `system.py`, `guard.py`,
`rules.py`, `tools.py`, `approval.py`, `protocol.py`.

## Composition and effects

Injects `session`, `session_launch`, `parent_permissions`, `tools`,
`client_events`, `variables`, `commands`, `settings`, `state`.
Provides `permissions` and `approval`, and owns the live approval waiter.
Registers the standard permission guard and human permission commands, observes
application/Agent configuration and `POLICY_CHANGED`, and registers
`request_permission` only for interactive sessions.

The permission service evaluates Tool names and argument regexes. Sandbox
policy is independent and remains a hard execution ceiling.

## Configuration and matching

```yaml
- id: permissions
  config:
    deny:
      - tool: shell
        params:
          command: "dangerous-command.*"
    allow:
      - tool: read
    ask:
      - tool: shell
```

Configured `tool` and `params` values use bounded regex full matching. A missing
constrained parameter does not match. Unspecified parameters are unrestricted.
Precedence is deny, allow, ask, default (ask). Explicit denies always win.
Child policy intersects with the parent policy.

Optional `paths` applies to all filesystem path arguments. An exact runtime
directory variable such as `${workspace}` means containment in that directory;
other values are regexes over resolved absolute paths. Variable substitutions
are escaped before insertion into regexes.

## Proactive authorization without execution

The Agent Tool is named `request_permission`; its approval channel and policy
are both owned by the `permissions` plugin. There is no separate approval plugin.

```json
{
  "tool": "shell",
  "params": {
    "command": "git status(?: --short)?",
    "cwd": "/work/project"
  },
  "reason": "Inspect status repeatedly during this task"
}
```

The request Tool treats `tool` as an exact name and validates parameter regexes.
It emits `PermissionRequestData` with `source="request_permission"` and
`permission={tool, params}`. It never invokes the target Tool.
A pending Tool's ask interaction instead carries its concrete `tool_call`
and `source="permission_system"`.

An approval response has `decision: allow | deny` and `scope: once | session`.
For proactive requests, once grants the next matching call; session grants all
matching calls in the current Agent thread, including after resume. For a pending call, once authorizes
that call. Decline leaves authorization unchanged.

Once grants are in-memory. Session grants are persisted through
`ctx.state.namespace("permissions")`, under the `grants` key, and restored at
application initialization. Updates are serialized and written before becoming
active; repeated approvals do not add duplicate rules.
Here session means the current Agent thread (normally `agent`), not a shared
store for all threads. Subagents use the existing parent permission chain.
Human policy configuration is a separate persisted settings operation.
Normal session approvals retain shell command/cwd/escape mode or filesystem
mode/operation/path/destructive flags. File bodies are not permission patterns.

An escape request still needs `shell(sandbox_permissions="require_escalated",
justification=...)`. A grant cannot change sandbox mounts or turn a denied
filesystem Tool into an unsandboxed operation.

## Public service and events

Import `PermissionsPort` from `XBotv2.permissions`.
`check` and `explicit_allow` are read-only. `check_tool_call` is the guard's
authorization step and may consume once grants; do not call it as a preview.
Parent/child deny checks do not consume a parent grant. A later non-permission
guard can still reject a call after permission consumption; this is intentional
because once covers one permission-layer authorization attempt and the sandbox
is a separate hard ceiling.

Human commands distinguish policy configuration from approved grants:

- `/permission status` summarizes all sources;
- `/permission rules` shows effective and session-configured rules;
- `/permission grants` shows persisted thread grants with stable list indexes;
- `/permission list` shows rules and grants together;
- `/permission set <tool> <allow|deny|ask>` and `reset <tool>` edit session policy;
- `/permission revoke <index>` and `clear-grants` remove persisted approvals.

`PERMISSION_REQUESTED` carries `PermissionRequested(tool_call, client_event)`.
`PERMISSION_DECIDED` carries decision, scope, rule, request_id, and source.
Both approval entrypoints validate once/session responses and record terminal
decisions; the normal guard also emits `PERMISSION_REQUESTED`. No observer should
silently turn an approval into persisted policy or a sandbox mutation.

Permission regexes use bounded `regex.VERSION0` full matches: 4096 characters
per pattern, 1,048,576 per matched value, and 10 ms per match. A complete
top-level policy check shares a 50 ms / 1024-match aggregate budget across
parent, child, and explicit-escape checks. Limit failures raise and stop the
call; they must not become a nonmatching deny rule. Structured parameter values
use compact canonical JSON with sorted keys; strings keep unquoted matching.

Register Tools through `ctx.tools.register(Tool.from_function(handler))`;
the registry applies guards. Do not implement another approval check inside
the handler or dispatch a synthetic ToolCall to acquire permissions.

## Approval contracts

Import `ApprovalPort`, `ApprovalDecision`, `PermissionRequestData`, and
`PermissionResponseRequest` from `XBotv2.permissions`. Wire declarations live
in `permissions/protocol.py`; they contain no policy or persistence logic.
`ApprovalPort.request(event)` returns a validated `ApprovalDecision`, whose
fields are `decision: allow | deny` and `scope: once | session`. HTTP responses
add `request_id`. The client event remains named `permission_request`.

ApprovalService validates raw client responses. PermissionHandlers applies the
decision, persists session grants or installs proactive once grants, and emits
`PERMISSION_DECIDED` only afterwards. Cancellation/invalid responses do not
invoke the decision handler; terminal logs preserve the original failure.
Pending calls recheck current deny rules after approval, including parent rules.
Session close cancels pending waiters. An unanswered request is not lost while
its turn is live: `open_session(mode="resume")` replays it through
`pending_interactions` with `resume_supported: true`. A client that reconnects
rebuilds the dialog from that field; deciding when to give up on an unanswered
request is the client's responsibility, not a server-side timeout.

See [sandbox.md](sandbox.md) for OS enforcement.
