# Sandbox

The thread-scoped sandbox registers a monotonic tool guard, provides `SANDBOX`,
and executes shell and filesystem operations through bubblewrap. It depends on
thread paths, settings, runtime variables, commands, the tool registry and the
permissions approval service. Its policy tracks the effective configuration;
processes and mounts exist for the duration of execution. Physical filesystem operations and the worker entry point are provided by
`core.api.filesystem`; tool-to-operation mapping belongs to `coretools.api.filesystem`.

`SandboxConfig` controls enabled, network, workspace and external read/write
access, and resource rules. `/sandbox` inspects and updates session policy.
Interactive parent threads register `request_sandbox_access`: it submits a
named permission request through `ApprovalPort.request`, and applies an
approved resource rule to session settings. Subagents and noninteractive
threads do not register that tool. Sandbox enforcement itself does not ask for
approval, and permission grants do not change the sandbox ceiling.

Tool handlers resolve `SANDBOX` and call `run_shell`, `filesystem` or path
queries after the normal tool guards have run. Policy changes affect later
executions; running processes keep their mounts. See [configuration and usage](configuration.md) for defaults, commands and execution behavior.

```python
from XBotv2.sandbox.api.models import SANDBOX

resolved = ctx.require(SANDBOX).resolve_read_path("src/main.py")
```

The mounted policy shares ArtifactStore's managed-path resolver with permission
path scopes and coretools. Current-session artifact/cache roots are read-only
managed roots. Resolving a logical ref does not rewrite the ToolCall or bypass
guards, approval, mount policy, or process isolation.
