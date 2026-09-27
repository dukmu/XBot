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

### YAML configuration and variable expansion

Permission policies are part of the normal plugin configuration tree. The
shared configuration loader expands variables before plugin schema validation
and before constructing `PermissionPolicy`; the permissions plugin receives
plain, already-expanded strings and never expands variables itself.

Put the `permissions` entry in the normal plugin tree. The configuration
layers are loaded in this order: built-in tree, global
`<data-dir>/config/plugins.yaml`, workspace `<workspace>/.xbot/plugins.yaml`,
then the active session's `<data-dir>/sessions/<session-id>/config.yaml`.
Later layers patch earlier entries by plugin id and recursively merge config
mappings; later scalar/list values replace earlier values. Agent
`permission_policy` frontmatter is also expanded by the shared variable
service before it is validated as a policy.

Use `$${NAME}` for a runtime variable and `$${env:NAME}` for an environment
variable. Environment references resolve while configuration files are read;
runtime references resolve when the session configuration is assembled.
Expansion substitutes the variable value verbatim. It does not quote or escape
the value for a regex. A single-dollar `${...}` is ordinary text and remains
unchanged, so regex syntax containing braces is not mistaken for interpolation.
For example, `'$${workspace}/src/.*\.py'` expands the workspace prefix and
leaves the regex suffix for the permission matcher. The explicit Markdown
``var`` block used in Agent prompts is a separate prompt-substitution syntax.

### Rule matching

`tool_pattern`, every value in `param_patterns`, and `path_scope` are bounded,
full-match regular expressions. A rule matches only when its tool pattern
matches, every named parameter exists and matches, and its path scope matches.
Parameters omitted from `param_patterns` are unrestricted. Structured
parameter values are serialized as canonical JSON before matching.

`path_scope` is applied only to filesystem path arguments supported by the
Tool operation. Each such argument is resolved to an absolute path (relative
paths are based at the runtime workspace and symlinks are resolved), then the
regex must full-match that resolved path. If an operation has multiple path
arguments, all of them must match. It is a path regex, not a directory-root
field: use `'$${workspace}/.*'` to scope all paths under the workspace, or add
regex constraints for narrower scopes.

Example plugin configuration:

```yaml
- id: permissions
  name: permissions
  config:
    default_decision: ask
    rules:
      - tool_pattern: edit
        param_patterns:
          mode: '(?:write|replace|patch)'
        path_scope: '$${workspace}/(?:src|tests)/.*\.py'
        decision: allow
      - tool_pattern: shell
        param_patterns:
          command: 'git status(?: --short)?'
        decision: allow
      - tool_pattern: '.*'
        param_patterns:
          command: 'rm -rf /.*'
        decision: deny
```

An environment value is written in the same YAML string form, for example
`path_scope: '$${env:PROJECT_ROOT}/src/.*'`; it must be set when configuration
is read. Runtime references such as `$${workspace}` are resolved when the
active session/thread's plugin tree is assembled. Expansion applies to string
values in nested plugin configuration, not mapping keys. A reference to an
unknown runtime variable or an unset environment variable fails loading with
an error rather than being kept or silently treated as empty.

The configuration layer expands `$${workspace}` before validation; the
permission rule then matches the resolved absolute path. Regex metacharacters
in the inserted value retain their regex meaning by design. Patterns are
compiled when installed and matching has pattern-size, input-size, time, and
aggregate-call limits. Invalid patterns or exhausted budgets raise a clear
error; they are never treated as a non-match that grants access.

### Decision precedence

Within one policy, a matching explicit deny wins. Otherwise a matching allow
wins over a matching ask; if neither matches, `default_decision` applies.
Across policies, deny wins, then ask, and the call is allowed only when all
policy layers allow it. Parent Agent policy is another restrictive layer.
Matching once/session grants can satisfy a call unless an explicit deny rule
matches; a parent layer can still reject it. A one-shot grant is consumed only
after the execution guard reaches a final allow. A session grant is persisted
in the current Agent thread's `permissions` StateService namespace and is
restored when that thread resumes.

`PermissionsPort.check()` is a read-only policy query. The registered
permission guard owns authorization and consumes a one-shot grant only after
its final decision is `allow`; Tool handlers must not repeat permission checks.
Later sandbox or other guards can still reject an allowed call. Approvals do
not widen sandbox policy. A Tool that can execute outside the sandbox declares
its per-call `sandbox_escape` predicate on `Tool`; permission and sandbox
plugins consume that declaration without copying Tool names or argument
values. A general allow does not implicitly authorize a declared escape; an
explicit allow must constrain the Tool-declared escape argument. The shell's
`sandbox_permissions=require_escalated` argument is shell execution behavior,
not a permission-rule field and is not duplicated in YAML policy.

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
