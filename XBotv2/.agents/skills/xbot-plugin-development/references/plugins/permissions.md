# `permissions`

Owns Tool-call authorization, permission policy, and live permission approvals.
Sandbox policy is a separate execution ceiling. Register Tools through the
standard `ctx.tools` path so the permission guard evaluates them.

- **Import/profile:** `XBotv2.permissions`, Agent profile.
- **Source:** `permissions/contracts.py`, `system.py`, `guard.py`, `rules.py`,
  `tools.py`, `approval.py`, and `protocol.py`.
- **Provides:** `permissions` (`PermissionsPort`) and `approval` (`ApprovalPort`).
- **Tool:** `request_permission` is registered only in interactive sessions;
  it requests a rule for future matching calls and never invokes the named Tool.
- **Commands:** `/permission` inspects and updates session policy/grants.
- **Routes:** the session protocol owns
  `POST /sessions/{session_id}/threads/{thread_id}/interactions/permission-response`.

## Policy contracts

```python
class PermissionRule(BaseModel):
    tool_pattern: str
    param_patterns: dict[str, str] = {}
    path_scope: str | None = None
    decision: Literal["allow", "deny", "ask"]

class PermissionPolicy(BaseModel):
    rules: tuple[PermissionRule, ...] = ()
    default_decision: Literal["allow", "deny", "ask"] = "ask"
```

Tool names and constrained argument values use bounded full-match regular
expressions. Every constrained parameter must exist and match; parameters not
mentioned by the rule are unrestricted. Structured values are matched as
canonical JSON. `path_scope` applies only to supported filesystem path
arguments. Matching limits are enforced and a limit error stops authorization;
it is not treated as a successful non-match.

Across policy layers, any applicable deny wins; absent a deny, an ask at any
layer requires approval; calls are allowed only when every applicable layer
allows them. Explicit grants are considered after deny rules. A one-shot grant
is consumed by one matching permission authorization attempt. A session grant
is persisted in the current Agent thread's `permissions` StateService namespace
and is restored when that thread resumes. Parent permission constraints apply
to child Agents.

`PermissionsPort.check()` is a read-only policy query.
`check_tool_call()` is the authorization path and may consume a one-shot grant.
Do not use it to preview a call or repeat permission checks inside Tool
handlers. Later sandbox or other guards can still reject an allowed call.
Approvals do not widen sandbox policy; shell sandbox escape still requires its
separate explicit escalation contract.

## Typed approval interaction

`PermissionRequest` is a typed live interaction with `interaction_id`,
`source`, `reason`, `resume_supported`, and a discriminated subject:

- `ToolPermission(kind="tool", tool_call=...)` describes a concrete Tool call
  paused at an `ask` decision.
- `NamedPermission(kind="named", tool=..., params=...)` requests a rule for
  future calls, as used by `request_permission`.

The client response contract is:

```python
class PermissionResponseRequest:
    request_id: str
    decision: Literal["allow", "deny"]
    scope: Literal["once", "session"] = "once"
```

The route's `request_id` addresses the pending interaction. It is not a turn
ID. Approval decisions are `Allowed(scope="once"|"session")` or
`Denied(reason=...)`. Session snapshots expose pending interactions so a client
can rebuild unanswered dialogs on reconnect; event replay alone is not their
durable source. See [client-runtime.md](../client-runtime.md) for the client
contract.

## Human command

The `/permission` command accepts:

```text
/permission [status|list|rules|grants|set <tool> <decision>|reset <tool>|revoke <index>|clear-grants]
```

`status`, `rules`, and `list` report effective layers and the session override;
`grants` lists persisted session grants; `set` and `reset` modify the session
policy; `revoke` removes one indexed session grant; `clear-grants` removes all
session grants. This command is human-facing and is separate from the Agent
Tool and the permission-response route.

## Plugin guidance

- Use the standard Tool registry; do not add another approval prompt or
  synthetic Tool execution path.
- Use `ApprovalPort` with a typed `PermissionRequest` for a live decision.
- Keep user-input elicitation separate: it answers a question and does not
  authorize a Tool.
- Keep permission grants in the owning state namespace. They are not plugin
  configuration and do not alter the sandbox.
