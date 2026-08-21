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
the XBot or XCore repository layout yet.

## Start With Discovery

First determine how XBot is installed. A plugin is an external Python package,
so the relevant interpreter is the one that launches the user's `xbot` command,
not necessarily the interpreter in this skill's repository.

- **Source checkout:** from the checkout, prefer `.venv/bin/python` (POSIX) or
  `.venv/Scripts/python.exe` (Windows), otherwise use the checkout's `uv run`.
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
- For ownership, composition, and built-ins, read [xbot-architecture.md](references/xbot-architecture.md).
- For stable symbols and Tool/route contracts, read [xbot-api.md](references/xbot-api.md).
- For `Context`, `inject`, events, services, schemas, and lifecycle, read [xcore-api.md](references/xcore-api.md).
- For selecting an existing capability, read [builtin-components.md](references/builtin-components.md).
- For workflow, testing, and troubleshooting advice, read [usage-guidance.md](references/usage-guidance.md).

Also inspect the current source files named by the references. Documentation is
the map; the checked-out code and tests are the final contract.

## Beginner Workflow

1. State the plugin's user-visible job and whether it needs a Tool, command,
   prompt fragment, event observer, service, or protocol route.
2. Choose the owning package boundary. A plugin owns its implementation and
   state; shared declarations belong in the owner package's public root.
3. Declare required and optional services with `inject`. Required services
   gate activation; optional services are read with `ctx.get(name,
   strict=False)` and must have a no-service behavior.
4. Implement `apply(ctx, config=None)`. Register hooks, Tools, commands, and
   prompt fragments through `ctx`; register cleanup before opening resources.
5. Bind plugin services in a typed factory or closure before registering a
   Tool. Never use an arbitrary `injected={...}` dictionary or a service bag.
6. Add a focused behavior test using the selected runtime Python (`uv run
   pytest` for uv, or `python -m pytest` for pip). Test public
   behavior, schema, permission/guard behavior, unload, and failure rollback.
7. Add or update a plugin-tree entry only after the plugin works in isolation.
   Verify the profile, `name`, `id`, config, and service dependencies.
8. Run the focused test, architecture check, compile check, and diff check.
   Run broader suites when the plugin crosses core, provider, protocol, or
   session boundaries.

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

Do not claim success from a command that used a different interpreter, an
uncollected test, or a test that stopped before exercising the plugin. Record
the install mode, interpreter path, package origins, environment limits, and
tests not run in the handoff. For the full example and expected test shape, read
[first-plugin.md](references/first-plugin.md).
