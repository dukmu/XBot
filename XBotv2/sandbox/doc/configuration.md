# Sandbox: the execution ceiling

Tree id/import: `sandbox`, Agent profile. Composition lives in
`XBotv2/sandbox/impl/plugin.py`; policy and bubblewrap execution are in
`policy.py` and `bwrap.py`.

## Services and ownership

Required injection: `thread_paths`, `session`, `tools`, `data_root`,
`variables`, `workspace_root`, `commands`, `settings`.
Provides `sandbox` (SandboxPolicy), registers a monotonic Tool guard and
the human `/sandbox` command, observes `POLICY_CHANGED`, and contributes
the sandbox summary on `CONTEXT_BUILD_INPUTS_READY`.

The sandbox denies access outside its configured ceiling. It never asks for
permission. Tool permission approval does not mutate it. Trusted plugins may consume its capabilities. Interactive parent threads can request a resource rule through `request_sandbox_access`, subject to human approval.

## Configuration

`SandboxConfig` is owned by `XBotv2.sandbox.api.models` and passed directly to
the plugin's `apply()` method:

| Field | Default | Meaning |
|---|---|---|
| enabled | true | use bubblewrap for Shell/filesystem execution |
| network | true | share host networking; false isolates the network namespace |
| external_read | readonly | external path read access |
| external_write | deny | external path write access |
| workspace_read | allow | workspace read access |
| workspace_write | allow | workspace write access |
| resources | [] | explicit path/access rules |

Access values: `allow`, `deny`, `readonly`, `readwrite`. There is no
sandbox `ask` state. Resources have `path: str` and `access`; paths can
use `$${workspace}`, `$${data_dir}`, and other existing RuntimeVariables.
The most-specific resource path wins, with the first rule winning ties.

Human commands:
- `/sandbox status`
- `/sandbox resources`
- `/sandbox set network false`
- `/sandbox set external_write deny`
- `/sandbox add readonly /path/to/reference`
- `/sandbox remove 1`
- `/sandbox reset [key]`

These call the settings service, persist the explicit session policy, and emit
`POLICY_CHANGED`. Resource indexes refer only to the current session overlay;
inherited resources are displayed separately and cannot be removed by index.
They affect later Tool invocations; an already running process keeps its
original mounts.

## Capability API

`SandboxPort` exposes `run_shell`, `filesystem`, path resolution and access checks. `SandboxPolicy` additionally provides these implementation methods:

- `run_shell_to_file(command, output, *, shell=None, cwd=None)` captures original combined bytes from launch and returns the exit code for shell tools.
- `run_shell(command, *, shell=None, cwd=None)` returns text for skill prompt preprocessing.
- `filesystem(operation, args)`
- `resolve_read_path(path)`, `resolve_write_path(path)`
- `check_filesystem_access(operation, args)`
- `check_tool_access(tool_name, args)`
- `describe()`, `export_config()`

A Tool registers through the standard tool service so guards run before execution. It
then uses the sandbox capability for I/O. Calling the handler directly or raw
`os`/`subprocess` does not gain isolation merely because a Tool was registered.

Only `shell` exposes `sandbox_permissions="require_escalated"`, with a
nonempty `justification`. The permission guard approves escape. Ordinary
`allow: shell` does not authorize it; an explicit approved parameter rule
must constrain escape mode. Read/edit/path Tools have no escape parameter.
A missing/failed bubblewrap backend fails closed.

## Paths and OS behavior

All relative paths, including `session/...`, are workspace-relative.
Artifacts use absolute paths resolved by ArtifactStore. Runtime directory
values come from the existing `RuntimeVariables.for_thread(...)` instance;
do not add another path service or a context hook.

The host root is normally read-only. With external reads denied it is hidden
and explicit read-only mounts supply system executables/libraries, Python's
base installation, and selected resolver/TLS files. Workspace/data/resources
retain their explicit mounts. This intentionally exposes runtime dependencies.

`/tmp` is a private tmpfs per invocation. Use the workspace for cross-call
temporary files. Runtime data and workspace `.xbot` remain read-only; a writable
workspace gets the directory created before its read-only mount is installed.
Denied resources are masked. Permission grants do not change these mounts.

Bubblewrap isolates Tool subprocesses, not the server or trusted Python plugin
process. Shared networking can reach host services. Read-only sockets are not
a service authorization boundary. Do not claim hostile-plugin isolation.

## Testing and related contracts

Use real bubblewrap to verify mount behavior, denied writes, and explicit escape;
a mocked policy tests only decisions. In a restricted outer sandbox, report
namespace restrictions and use the approved test environment.

See [permissions configuration](../../permissions/doc/configuration.md) for authorization and approval, and [session reference](../../.agents/skills/xbot-plugin-development/references/plugins/session.md) for path and identity ownership.
