# Permissions

The thread-scoped component evaluates layered policies, registers a final guard
with the standard tool service, and owns approval interactions. It depends on
settings, the tool registry, interaction transport, state, and Agent selection.
It holds transient one-shot grants and persists session grants in the thread's
`permissions` state namespace; closing a session cancels pending approvals.

`PermissionPolicy` configures regular-expression rules and a default decision.
An explicit deny wins; parent policies remain restrictive. The guard consumes a
one-shot grant only on allow. `/permission` inspects and updates session policy
and grants. `request_permission` is available in interactive sessions and asks
for authorization of future calls. Approvals never widen sandbox policy.

Consumers use `PERMISSIONS` for `check()` or `APPROVAL` for a typed
`PermissionRequest` and await `request()`. The plugin publishes typed permission
request and decision events. Clients respond through the existing interaction
route. See [configuration and usage](configuration.md) for policy layers, matching, commands and approval interactions.

```python
from XBotv2.permissions.api.models import PERMISSIONS

decision = ctx.require(PERMISSIONS).check("shell", {"command": "git status"})
```

This query does not consume a grant; the registered tool guard owns authorization.

Permissions handles its own persisted grants during session fork.

When evaluating a filesystem `path_scope`, the mounted policy resolves managed
artifact IDs and cache paths through the common ArtifactStore path contract.
The original argument patterns and approval payload are unchanged. Sandbox and
actual read execution use that same physical target; runtime materialization
pins grant lifetime only, not permission or sandbox escape.
