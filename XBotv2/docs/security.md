# Security model

This page records the boundary rules. The executable schemas, guard order,
approval payloads, regex examples, and test patterns are in the skill's
[permissions](../.agents/skills/xbot-plugin-development/references/plugins/permissions.md)
and [sandbox](../.agents/skills/xbot-plugin-development/references/plugins/sandbox.md)
references.

## Two independent controls

The sandbox is a hard capability ceiling: mounts, network, workspace access,
runtime data visibility, and host execution are decided by sandbox policy.
The permission system is a Tool-call policy: it matches the exact Tool name
and parameter values using bounded full-match regular expressions.

Approval cannot widen the sandbox. If a sandbox guard rejects a call, that
permission-layer attempt is consumed as an authorization attempt but execution
does not occur.

## Permission requests and approvals

`request_permission` asks for a future rule and never executes its target Tool.
It carries a Tool name, parameter patterns, and a reason. A running Tool's
`ask` decision carries the actual Tool call; these are deliberately different
payloads and share only the approval transport.

- `once`: one matching permission authorization attempt.
- `session`: persisted in the current Agent thread's permissions namespace;
  it survives restart/resume of that thread and is inherited by child Agents
  through the parent permission chain.
- `deny`: always wins over an allow at the same effective policy layer.

`check()` is a read-only policy query. `check_tool_call()` is the consuming
authorization path. It must not log raw command bodies, patch content, or
permission regexes. Audit logs record request id, source, outcome, and failure
class.

## Sandbox and cwd

The shell workspace is the default cwd. Omitted cwd and the explicit workspace
cwd are normalized consistently. Host execution requires
`sandbox_permissions=require_escalated` and a nonempty justification. The
human `/sandbox` command and the authenticated session-policy route are the
configuration surfaces for changing the policy; an Agent cannot rewrite the
policy through shell text.

## Paths

`RuntimeVariables.for_thread(...)` and `ArtifactStore.model_path()` provide
absolute model-facing paths. Artifact IDs remain logical and persisted. A
relative Tool path resolves inside the workspace; `session/...` is an ordinary
workspace path, not a virtual storage prefix.

## Limits

Bubblewrap protects Tool subprocesses, not trusted Python plugins or the XBot
server itself. A malicious installed plugin can use host I/O. This is a
capability boundary for Agent Tools, not an untrusted-code sandbox.
