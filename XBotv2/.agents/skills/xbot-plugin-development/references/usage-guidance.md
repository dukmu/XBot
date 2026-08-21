# Development and Usage Guidance

## Make the First Version Small

Start with one user-visible behavior and one public registration. Keep a
service/Tool factory close to the plugin that owns the dependency. Add an
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

## Common Failure Modes

| Symptom | Check |
|---|---|
| Plugin remains pending | A required `inject` service is absent or not running. |
| Optional feature crashes | It used `ctx.require` or attribute access instead of `ctx.get(..., strict=False)`. |
| Tool schema exposes a service | The service was left in the model function signature; bind it in a closure. |
| Tool loses call identity | Use a keyword-only `ToolCall`, not a session id or custom context. |
| Duplicate tool error | Choose a unique display name and namespace; do not replace another owner. |
| Unload leaks a dynamic Tool | Record the returned registration name and unregister it in owner cleanup. |
| Permission or sandbox behavior differs | The Tool bypassed the standard registry/guard/capability path. |
| Test passes locally but not in XBot | The test used a different interpreter, installed a second XBot copy, or injected a checkout `PYTHONPATH` into a wheel-based project. |

## Before Sharing or Committing

Inspect the actual mounted tree, public package exports, and the diff. Run the
focused tests, architecture checker, compile check, and `git diff --check`.
Document configuration and public behavior changes. Do not include generated
runtime sessions, caches, logs, web bundles, or virtual environments.
