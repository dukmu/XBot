# Development and Usage Guidance

## Make the First Version Small

Start with one user-visible behavior and one public registration. Keep a
named service or Tool handler close to the plugin that owns the dependency. Add an
event or service only when two components need a stable contract; do not add
an abstraction for a single private call.

## Choose the Right User Surface

- The Agent needs to perform an operation: Tool returning `ToolResult`.
- A human needs a direct control: slash `Command`, not a synthetic ToolCall.
- A client needs a route or stream event: owning protocol package.
- Another plugin needs a capability: declared XCore service.
- A model context needs stable instructions: prompt/context-builder API.

## Test in Layers

Run a pure function/schema test first. Then mount the plugin with a minimal
XCore context and test registration, event behavior, and cleanup. Add a real
application or transport test only when the feature crosses those boundaries.
Use the selected XBot runtime for all layers. For `uv`, use `uv run pytest`
from the plugin project; for `pip`, use `python -m pytest` after activating the
same virtual environment that owns `xbotv2`. Never mix a global pytest with a
project environment. Before a report, record `sys.executable`, `XBotv2.__file__`,
and `xcore.__file__`.

Use this progression and stop at the last boundary the feature crosses:

1. schema/domain unit test;
2. real XCore mount, activation, behavior, and unload;
3. plugin-tree import and profile validation;
4. XBot application test with a disposable data directory/workspace;
5. HTTP/TUI/Web/client test only for transport or interaction behavior;
6. real provider/browser/network smoke when the external behavior is essential
   and credentials/capabilities are available;
7. wheel build, clean install, import provenance, and bundled asset check.

For state, construct a second Context on the same data directory and assert
recovery. For dynamic registration, fail in the middle and assert rollback.
For resources, assert close/dispose. For permissions and sandboxing, invoke the
standard pipeline rather than only the handler.

## Common Failure Modes

| Symptom | Check |
|---|---|
| Plugin remains pending | A required `inject` service is absent or not running. |
| Optional feature crashes | Absence was not a real feature mode; make the service required, or resolve the optional mode once during composition. |
| Tool schema exposes a service | The service was left in the model function signature; pass it to a named handler constructor. |
| Tool loses call identity | Use a keyword-only `ToolCall`, not a session id or custom context. |
| Duplicate tool error | Choose a unique display name and namespace; do not replace another owner. |
| Unload leaks a dynamic Tool | Record the returned registration name and unregister it in owner cleanup. |
| Permission or sandbox behavior differs | The Tool bypassed the standard registry/guard/capability path. |
| Test passes locally but not in XBot | The test used a different interpreter, installed a second XBot copy, or injected a checkout `PYTHONPATH` into a wheel-based project. |
| State appears in the wrong directory | The plugin wrote a path itself instead of using `ctx.state.namespace(...)`. |
| Plugin imports in tests but not XBot | Compare `sys.executable`, `command -v xbot`, package `__file__`, and editable install metadata. |
| Plugin object retains prior runtime | The loader creates fresh no-argument plugin instances; move runtime fields into an apply-created owner. |
| Unknown config keys remain in validated data | Use `.strict()` to discard them; XCore strict strips unknown keys rather than rejecting them. |
| Stop leaves a client/process alive | Register cleanup immediately after acquisition and test partial apply failure. |
| Consumer needs hand-written reload | Publish a service and declare required `inject`; let XCore roll consumers between running and pending. |
| State resets after restart | Check `--data-dir`, namespace/key/version, and the fail-loud deserializer. |
| Model sees Tool but call is denied | Registration worked; inspect permission/sandbox through the standard pipeline. |
| Wheel works from checkout only | Install it cleanly and verify non-Python package data. |

## Review Architecture, Not Only Output

Search production code for these warning signs and justify every exception:

- `self.ctx` or Context stored past `apply`;
- `ctx: Any` on business services rather than only the plugin boundary;
- `getattr(ctx, ...)`, repeated `ctx.get`, or `if dependency is None` for a
  required service;
- nested handlers/closures that capture plugin state;
- hand-built paths beneath a data directory or `plugin_state`;
- manual message/history serialization or duplicated persisted facts;
- sibling implementation imports instead of owner-root contracts;
- callback holes, reload events, or startup ordering that duplicate XCore
  dependency activation;
- dynamic registration without rollback and cleanup;
- protocol/client behavior mixed into Tool/domain code.

Passing tests do not excuse these structures. Confirm one clear owner, one
canonical persisted representation, explicit dependencies, and reversible
effects.

## Packaging Checklist

- declare supported Python and XBot versions;
- document distribution name versus import package;
- include `py.typed` when publishing typing support;
- declare package data for prompts, templates, schemas, skills, and references;
- inspect the built wheel;
- install it into a clean uv/pip environment without checkout `PYTHONPATH`;
- verify plugin/XBot/xcore origins, mounted lifecycle, and intended profile;
- exclude caches, sessions, data directories, logs, and credentials.

## Before Sharing or Committing

Inspect the actual mounted tree, public package exports, and the diff. Run the
focused tests, architecture checker, compile check, and `git diff --check`.
Document configuration and public behavior changes. Do not include generated
runtime sessions, caches, logs, web bundles, or virtual environments.
