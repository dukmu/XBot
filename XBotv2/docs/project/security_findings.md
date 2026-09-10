# Runtime boundary findings — 2026-09-06

## Corrected in the working tree

- Virtual `session/` intercepted real workspace paths. Resolution now uses
  ordinary absolute/workspace-relative paths and the existing RuntimeVariables.
- Attachment rendering fabricated a path from the logical artifact id. Both
  providers now resolve it through the bound ArtifactStore.
- Shell allow-session rules dropped command/cwd and widened later execution.
  Generated grants now retain those constraints and filesystem operation/mode.
- One-shot escape grants were consumed before the explicit-escape check.
  The check now precedes consumption.
- Session approval events wrote runtime grants into config YAML. That writer
  has been removed. Session grants now persist in the owning Agent thread's
  `permissions` state namespace and survive restart; once grants remain volatile.
- Host `/tmp` was writable inside all sandbox shells. It is now a private tmpfs.
- Denied resource mounts were omitted, leaving content visible through the host
  root. They are masked; a denied external root uses explicit runtime mounts.
- Equal-path resource mount precedence differed from Python checks. Both now
  use the first rule at the most-specific path.
- HTTP/framework logs shared domain output or were suppressed. Transport records
  now use a separate rotating file; uvicorn preserves the application setup.
- Self-reference docs incorrectly described an enclosing Agent sandbox, escape
  on edit, virtual paths, and configuration-persisted approvals. Those descriptions
  have been corrected against the executable contracts.
- A writable workspace without `.xbot` allowed a shell to create a future
  configuration overlay. Sandbox setup now creates and read-only mounts that
  directory; filesystem tools also reject writes to that namespace.
- Cached Tool prose embedded thread paths. New cache envelopes persist logical
  artifact IDs; provider requests resolve their path through the current store,
  without changing the original message or append-only trajectory.

## Remaining design limits requiring explicit follow-up

- `check()` is read-only, and child denial does not consume parent grants. A
  permission allow-once is deliberately consumed at the permission guard even
  if a later sandbox guard rejects the call: it represents one authorization
  attempt and cannot weaken the sandbox ceiling.
- Pending calls now recheck current deny rules after approval. Persisted grants
  may still remain under an overriding deny rule; deny does not delete grants.
- Permission parameter patterns operate on flattened scalar text or canonical
  JSON for a structured value. They are not a nested-JSON predicate language.

- Python plugins execute in the trusted server process. Registry guards alone
  cannot isolate a malicious plugin that performs direct host I/O.
- Sandbox network-enabled mode shares host networking. It is not a per-domain
  egress firewall or isolation from host network services.
- Arbitrary historical text quoting paths is not rewritten. Request-time
  projection applies only to marked cache envelopes with an artifact reference;
  user text and unmarked Tool output retain their original meaning.
- External-read deny still exposes the documented runtime mounts and explicitly
  mounted data/workspace resources. Do not describe it as zero host visibility.

## Structural review — addressed on 2026-09-08

The five findings were confirmed in source and addressed without a second
executor or an execution-transaction framework:

1. Removed manually hashed grant IDs. A locked `grants` snapshot uses the
   existing thread StateService namespace. Concurrent grants are retained and
   duplicate approval is idempotent; live updates do not reload all state.
2. Removed deny/ask collections for session grants and the competing in-memory
   `PermissionsService.add_rule` entrypoint. The runtime service owns grants;
   the same service serializes durable updates. The handler only translates a
   validated decision into that service operation and publishes the result.
3. Renamed the effectful operation to `apply_decision`. It owns both proactive
   once grants and persistent session grants; audit completion follows application.
   Failed writes leave the live policy unchanged.
4. Removed the separate permission_request plugin/package and standalone
   one-model file. Permissions now registers its approval channel, waiter,
   guard and Tool. `permissions/protocol.py` declares the shared decision and
   wire contracts; ApprovalService returns the validated result.
5. Consolidated terminal logging in a finally block. Invalid responses and
   cancellation do not invoke an effectful handler to fabricate a denial.
   Original exceptions survive, and no second recording exception replaces them.

The unused `_KEEP_PARENT` reparenting path and duplicate parent field were also
removed. Existing configuration-boundary mapping/model conversion remains;
it is not a reason to reintroduce context bags or another state abstraction.

The follow-up command and matcher review also added human-readable policy and
grant listings, indexed session-grant revocation, session sandbox resource
management, canonical structured-argument matching, and one aggregate regex
budget shared by a complete parent/child/escape decision.

No compatibility import/plugin alias is retained. Old experimental hash-keyed
permission state is rejected explicitly rather than silently ignored or widened;
no automatic conversion or deletion of existing user state is performed.

## Verification

Core/integration run after plugin consolidation: **857 passed in 68.50 seconds**, using
`PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/core XBotv2/tests/integration -q`.
This includes 22 boundary/decision regressions with real bubblewrap checks, actual
application restart/thread-isolation checks, and both provider cache-path
projections. The bundled skill validator and
`git diff --check` also passed.

After the final grant ownership, command, and aggregate-matcher changes, the
application startup, public API, permission, decision, sandbox, HTTP command,
and TUI status selections were rerun: **113 passed in 4.69 seconds**. The user
also reported the complete core/integration suite passing; its exact terminal
summary was not captured in this document.

Restricted execution previously stalled in the browser tests; that run was
interrupted and rerun with approval for local networking. No live model endpoint
or hostile-plugin isolation test was run; these checks do not establish either.
User changes in `evaluation/run_harnessbench.py` are outside this task.
