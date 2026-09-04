---
name: xbot-plugin-development
description: Guide a new or experienced developer through creating, testing, and reviewing XBot plugins with the matching XCore APIs and runtime boundaries.
metadata:
  short-description: Build and test XBot plugins
---

# XBot Plugin Development

Use this skill for an XBot plugin project: starting a plugin, adding a Tool or
command, connecting to a service, subscribing to events, adding configuration,
or reviewing an existing plugin. It is written for developers who may not know
the XBot or XCore repository layout yet. The references are versioned with the
XBot distribution; when a checkout is available, source and tests remain the
final authority.

## Read the Fast References First

Do not begin by grepping the whole repository. Read the small index that
matches the work, then follow the detailed page only when necessary:

1. [plugins_list.md](references/plugins_list.md) for the bundled tree, profiles,
   injected services, public operations, and the built-in plugin index;
2. [xbot-architecture.md](references/xbot-architecture.md) for ownership,
   application composition, session/runtime boundaries, and persistence;
3. [xcore-api.md](references/xcore-api.md) for Context, effects, events,
   injection, schemas, and lifecycle;
4. [first-plugin.md](references/first-plugin.md) for the complete package,
   install, test, overlay, and smoke-test path;
5. [extension-patterns.md](references/extension-patterns.md) for Tool,
   command, event, service, state, and resource patterns.

Read [session-trace.md](references/session-trace.md) for JSONL/history work.
For a bundled component, open its page under
[`references/plugins/`](references/plugins/README.md) after selecting the
component from the index. These pages record the current service names and
effects; they are not permission to import private implementation modules.

## Start With Discovery

First determine how XBot is installed. A plugin is an external Python package,
so the relevant interpreter is the one that launches the user's `xbot` command,
not necessarily the interpreter in this skill's repository. The data directory
is also an explicit runtime input: `~/.xbot` is only the default. Tests and
smoke runs should normally pass a disposable `--data-dir`; do not assume that
the user's home directory is writable or that `.xbot` means home.

- **Source checkout:** from the checkout, prefer `.venv/bin/python` (POSIX) or
  `.venv/Scripts/python.exe` (Windows), otherwise use the checkout's `uv run`.
  When importing source without installing it, use `PYTHONPATH` for the
  checkout roots only:

  ```bash
  PYTHONPATH="$XBOT_ROOT:$XBOT_ROOT/XCore" .venv/bin/python -c \
    'import XBotv2, xcore; print(XBotv2.__file__, xcore.__file__)'
  ```

  This is a source-checkout technique, not a way to test a published wheel.
- **uv project dependency:** run every command as `uv run ...` from the plugin
  project's directory. Add the runtime with `uv add xbotv2` (or an editable
  local path while developing XBot itself).
- **pip environment:** create/activate a virtual environment, then use
  `python -m pip install xbotv2 pytest`. Do not use bare `pip` or a global
  `pytest`; `python -m pip` and that same `python` must own the installation
  and tests.

If the project uses `uv pip` rather than a `pyproject.toml`, use
`uv pip install xbotv2 pytest` against the project's active environment and
run tests with that environment's `python -m pytest`. `uv pip` is an installer,
not a separate runtime: verify the resulting `sys.executable` exactly as for
pip.

Before testing, print the runtime provenance and verify both public packages:

```bash
python -c 'import sys, XBotv2, xcore; print(sys.executable); print(XBotv2.__file__); print(xcore.__file__)'
# uv projects: use `uv run python -c "..."` instead of `python -c ...`
```

When a checkout is available, set `XBOT_ROOT="$(git rev-parse --show-toplevel)"`
and inspect the source/docs there. With an installed wheel, use the bundled
references in this skill and inspect package resources with
`importlib.resources`; do not assume a Git checkout or a filesystem path such
as `/home/...` exists. The invariant is that tests, the plugin loader, and
`xbot` all import `XBotv2` and `xcore` from the same environment. Read the
references progressively:

- For a first plugin and a complete example, read [first-plugin.md](references/first-plugin.md).
- For Tool, command, event, service, state, and resource-owner patterns, read
  [extension-patterns.md](references/extension-patterns.md).
- For ownership, composition, and built-ins, read [xbot-architecture.md](references/xbot-architecture.md).
- For stable symbols and Tool/route contracts, read [xbot-api.md](references/xbot-api.md).
- For `Context`, `inject`, events, services, schemas, and lifecycle, read [xcore-api.md](references/xcore-api.md).
- For selecting an existing capability, read [builtin-components.md](references/builtin-components.md).
- For workflow, testing, and troubleshooting advice, read [usage-guidance.md](references/usage-guidance.md).

For an installed wheel, the same command should show `site-packages` origins
for both packages and must not contain a checkout `PYTHONPATH`. In a restricted
or offline sandbox, a plugin source tree may be exposed with `PYTHONPATH` and
mounted through the Python `start_application(..., plugin_dirs=[...])` API; the
current `xbot` CLI does not expose `plugin_dirs`. For a normal user install,
install the wheel into the environment that owns `xbot` and select it from a
workspace `.xbot/plugins.yaml` entry.

Also inspect the current source files named by the references. Documentation is
the map; the checked-out code and tests are the final contract.

## Beginner Workflow

1. State the plugin's user-visible job and whether it needs a Tool, command,
   prompt fragment, event observer, service, or protocol route.
2. Choose the owning package boundary. A plugin owns its implementation and
   state; shared declarations belong in the owner package's public root.
3. Declare required services with `inject`; XCore gates activation until every
   dependency exists. Use an optional dependency only for a documented feature
   mode where absence is valid, and resolve it once at the composition boundary.
4. Implement `apply(ctx, config=None)` as composition: read declared services,
   construct typed runtime objects/handlers, and register their named methods.
   Business services must not retain the whole `Context`.
5. Pass Tool dependencies to a named handler or service before registration.
   Avoid business closures, service bags, runtime dependency probing, and
   defensive `None` fallbacks for required services.
6. Add a focused behavior test using the selected runtime Python (`uv run
   pytest` for uv, or `python -m pytest` for pip). Test public
   behavior, schema, permission/guard behavior, unload, and failure rollback.
7. Add or update a plugin-tree entry only after the plugin works in isolation.
   Verify the profile, `name`, `id`, config, and service dependencies.
8. Run the focused test, architecture check, compile check, and diff check.
   Run broader suites when the plugin crosses core, provider, protocol, or
   session boundaries.

If a mounted plugin is `FiberState.PENDING`, inspect
`handle.missing_dependencies` before changing code. The usual test fixtures
provide `RuntimePaths.from_data_dir(tmp_path / "data")` as `runtime_paths`, a
workspace `Path` as `workspace_root`, `paths.data_dir` as `data_root`, and a
typed `LoopState`/`SessionLaunch`/`ThreadPaths` only when the plugin declares
those services. Do not satisfy a missing service with `None`; add the service
at the composition boundary or narrow the plugin's declared dependency.

Do not skip directly from a code snippet to editing the user's global plugin
tree. A first plugin is complete only when all of these checkpoints have
observable evidence:

1. **Import:** the package imports from the same interpreter as `xbot`.
2. **Composition:** a real XCore `Context` activates the plugin with its
   required services, and missing services leave it pending or fail tree
   validation clearly.
3. **Behavior:** the registered Tool, command, event listener, or service does
   the promised work through its public contract.
4. **Cleanup:** stop/dispose removes registrations and closes owned resources.
5. **Persistence:** if state is used, a new Context over the same data directory
   reads the JSON-compatible value through the same namespace.
6. **Application:** the intended XBot profile loads the tree entry and exposes
   the capability through the real client boundary.
7. **Distribution:** a built wheel contains the plugin package and any bundled
   skill/reference assets; a clean environment can install and import it.

The XBot distribution includes this skill as package data. On application boot,
`ensure_initial_config()` copies it to
`<data-dir>/.agents/skills/xbot-plugin-development/`; that target is governed
by `--data-dir`/`XBOT_DATA_DIR`, not necessarily `$HOME`. Unrelated skills in
that directory are preserved.

For each checkpoint, prefer one small assertion that observes the contract.
Do not inspect a private list merely because it is easier than invoking the
registered behavior.

## Stable Design Rules

- Use package-root exports and typed events/operations for cross-plugin APIs;
  do not import a concrete sibling plugin.
- Keep protocol routes and wire models in the owning `protocol.py`; do not put
  transport concerns in Tool or service contracts.
- Use the producer-owned typed event for business facts. `EventContext` is a
  narrow loop hook payload, not a general application context.
- A Tool may declare one keyword-only `ToolCall` parameter. Core omits it from
  the provider schema and passes the final rewritten call. Do not invent a
  one-field invocation context or pass a session id separately.
- Runtime-discovered registrations must be tracked and explicitly removed by
  their owner. Discovery should be idempotent and transactional.
- Persist plugin state with `ctx.state.namespace("plugin-name")`; keep runtime
  handles, waiters, and clients out of persisted state.
- Treat plugin configuration as immutable startup input. Never write runtime
  state into configuration or construct paths under `plugin_state`; the shared
  StateService owns validation, serialization, locking, and physical layout.
- XBot has no runtime reload contract. Let XCore dependency activation compose
  the application once, and use ordinary typed events for runtime facts.
- Reuse built-in services and the standard Tool/guard/event path. Do not create
  a second executor, permission bypass, private wakeup callback, or hard-coded
  dependency on a built-in plugin name.

## Verification

Use the selected runtime for every command. Only add `PYTHONPATH` for a source
checkout when its own test instructions require it; an installed `xbotv2`
wheel must be tested without checkout path injection:

```bash
python -m pytest path/to/test_plugin.py -q
python -m compileall -q path/to/plugin_package
git diff --check
```

For a uv project, replace `python -m ...` with `uv run ...`. Architecture
checks and source-wide compile checks are checkout-only; for an installed
package, verify imports, plugin-tree loading, and the plugin's own tests.

Before changing the real data directory, use a disposable data directory and
workspace:

```bash
mkdir -p /tmp/xbot-plugin-smoke/data /tmp/xbot-plugin-smoke/workspace
xbot once --data-dir /tmp/xbot-plugin-smoke/data \
  --workspace /tmp/xbot-plugin-smoke/workspace "Use the hello tool for Ada"
# uv project: prefix with `uv run`; pip: use the active environment's `xbot`.
```

Copy the plugin overlay into that workspace only after the package import and
mounted tests pass. A model response is not sufficient evidence by itself:
inspect startup errors and confirm the expected Tool/command registration or
client event was actually exercised.

Do not claim success from a command that used a different interpreter, an
uncollected test, or a test that stopped before exercising the plugin. Record
the install mode, interpreter path, package origins, environment limits, and
tests not run in the handoff. For the full example and expected test shape, read
[first-plugin.md](references/first-plugin.md).
