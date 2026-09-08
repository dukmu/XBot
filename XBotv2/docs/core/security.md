# Sandbox, permissions, and runtime paths

Sandbox policy is the execution ceiling. Permissions authorize Tool names and
parameters within that ceiling. Approving a permission never modifies mounts,
network isolation, or sandbox configuration.

## Human control and Agent requests

The `permissions` plugin owns both the policy and live approval channel. Its
`protocol.py` holds decision/request/response declarations; there is no separate
permission-request plugin. The wire event remains `permission_request`.

Only a human command/configuration operation changes sandbox policy. Use
`/sandbox status`, `/sandbox resources`, `/sandbox set <key> <value>`,
`/sandbox add <access> <path>`, `/sandbox remove <index>`, or
`/sandbox reset [key]`.
The settings service persists these explicit session changes and emits
`POLICY_CHANGED`; the sandbox consumes that event. `ask` is not a sandbox
access value. The supported access values are allow, deny, readonly, readwrite.

An Agent requests execution outside the sandbox only through the `shell` Tool
with `sandbox_permissions="require_escalated"` and a nonempty `justification`.
The standard permissions guard approves that call. A general `allow: shell`
does not approve escape: an explicit rule must constrain `sandbox_permissions`.
Filesystem Tools have no escape parameter. Missing bubblewrap fails execution;
there is no automatic unsandboxed retry.

`request_permission` requests authorization for **future** calls. Its schema is:

```json
{
  "tool": "shell",
  "params": {"command": "git status(?: --short)?", "cwd": "/work/project"},
  "reason": "Inspect repository status during this task"
}
```

`tool` is an exact name; every value in `params` is a full-match Python regular
expression. Unspecified parameters are unconstrained. The request carries
`PermissionRequestData.permission`; it never carries or executes a target
ToolCall. The same approval channel handles a running Tool's `ask` decision,
whose payload instead carries `tool_call`. Deny rules retain precedence.

Allow-once grants one matching future call for `request_permission`; for an
already pending Tool it authorizes that call. Allow-session persists a rule in
the current Agent thread's `ctx.state.namespace("permissions")`. It survives
configuration recomposition and restart/resume of that thread, without writing
configuration YAML. Subagents inherit through the existing parent permission
chain, not a new shared store. Ordinary shell session approval retains command,
cwd, and explicit escape mode. Filesystem approval retains operation/mode,
paths, and destructive flags, without storing document bodies.

Fresh threads do not share this state. The existing whole-session fork copies
thread state, so a fork carries an independent snapshot of persisted grants;
this change does not introduce a separate permission-inheritance mechanism.

Omitted/empty shell cwd is matched and saved as the current workspace, just as
the shell executes it. A different explicit cwd does not match that grant.
Both approval entrypoints validate `allow/deny` and `once/session`; invalid
responses fail closed. Logs record request and terminal decision using request
ID/source, including rejection, cancellation, timeout and validation failure,
without logging raw command arguments or permission regex bodies.

`check()` is a read-only policy query. `check_tool_call()` authorizes at the
permission guard and consumes a matching once grant only on allow. A child deny
does not consume its parent's grant. Pending calls recheck current deny rules
after the human response; once/session application precedes successful audit
completion. Later guards may still reject after permission consumption. This is
intentional: once means one permission-layer authorization attempt, while the
sandbox remains an independent hard ceiling.

`/permission status` summarizes the effective policy, session overrides, and
persisted approvals. `/permission rules` and `/permission grants` show their
separate sources; `/permission list` shows both. Persisted approvals can be
removed with `/permission revoke <index>` or `/permission clear-grants`.

Regex matching uses `regex.VERSION0`, with a 4096-character pattern limit,
1,048,576-character value limit and 10 ms timeout per match. One top-level
policy decision also has a shared 50 ms / 1024-match aggregate budget, including
parent-policy and explicit-escape checks. Invalid patterns and limit failures
raise, rather than skipping a deny rule. Compilation caches are bounded.
Structured Tool parameters are matched as canonical compact JSON with sorted
object keys; scalar strings retain their existing unquoted matching semantics.

## OS enforcement

Bubblewrap provides process/mount isolation. `/tmp` is private to each sandbox
invocation; it is no longer a writable bind of host `/tmp`. Use the workspace
for files that must survive between shell invocations. Workspace and resource
mounts follow configured access; denied resources are masked. More-specific
resource paths win; the first rule wins for an equal path. Workspace `.xbot`
configuration and runtime data remain read-only. When granting workspace write
access, setup creates `.xbot` if absent before mounting it read-only.

With external reads denied, the host root is hidden; read-only runtime mounts
provide `/usr`, shell/library locations, Python's base installation, and selected
resolver/TLS files under `/etc`. These runtime dependencies are intentional
exceptions, not user-file grants. Explicit workspace/resource/data mounts remain
visible. External write permission is a broad operator-controlled capability.

The server and trusted Python plugins themselves are not enclosed by Tool
bubblewrap. A Tool that uses raw host I/O can bypass this boundary; plugin
authors must use the sandbox capability. Network access, when enabled, also
allows contacting host services. This is not a hostile-plugin isolation system.

## Paths

`RuntimeVariables.for_thread(paths, workspace_root, thread_paths)` is the single
source of absolute runtime directories. The session passes that same immutable
object into `LoopState`; context construction reads it directly. No extra path
service or context hook is needed.

`ArtifactRef.id` stays a logical storage identity. `ArtifactStore.model_path()`
resolves it to the current thread's absolute filesystem path for request-time
cache markers and provider attachment prompts. Persisted Tool cache envelopes
retain logical IDs; both provider adapters project the path without modifying
history. Tool paths are absolute or workspace-relative;
`session/...` now means a real directory inside the workspace. There is no
reserved virtual prefix. Treat already persisted old cache prose as historical
text; do not rewrite the append-only trajectory. Artifact identity and runtime
variables are the authority when reopening old sessions.

See [runtime logging](logging.md) for transport/domain log separation and
[the implementation findings](../project/security_findings.md) for limits and
verification evidence.
