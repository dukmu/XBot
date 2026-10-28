# Development

Use the skill as the detailed development manual. This page keeps only the
repository-level workflow; plugin examples and XCore API details belong in the
skill so they are distributed with the matching runtime.

## Repository rules

Use `.venv`/`uv`, keep changes on a `dev-*` branch, preserve unrelated user
changes, and do not commit runtime sessions, logs, generated Web bundles, or
evaluation output. Do not add a compatibility shim merely to preserve an old
internal path.

## Verification

Run focused tests first, then the appropriate core/integration suite:

```bash
.venv/bin/pytest -q
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/integration -q
PYTHONPATH=XBotv2 .venv/bin/python scripts/check_architecture.py --scope all
PYTHONPATH=XBotv2 .venv/bin/python scripts/audit_plugin_contracts.py --format tsv
git diff --check
```

The architecture scripts are independent developer audits, not pytest tests.
They parse source with AST and do not import application modules. Migrated
plugins define public declarations in `api/`, keep implementation in `impl/`,
and expose no package-root facade. The audits check cross-plugin imports
against that boundary and report remaining legacy declarations separately.
The [core](../core/doc/design.md), [loop](../agentloop/doc/design.md) and
[context builder](../context_builder/doc/design.md) owner guides define their
current boundaries and remaining storage/runtime migration work.
Focused tests live beside their plugin; the default pytest discovery includes
them and the central integration/client suites. A green test suite does not prove interactive
Web/TUI behavior, provider interoperability, socket permissions, or
documentation accuracy.

## Embedded process composition

Use `XBotv2.main.open_runtime(paths=...)` for the same process startup used by
CLI, HTTP and ACP. It creates one root and mounts the loader's process facets.
Open a thread through `root.require(SESSIONS).open(OpenSession(...))`, which
returns its real Context. Resolve tools, loop execution, permissions or other
capabilities from their owning API keys on that Context.

Tests may pass `model_override` in `OpenSession` or register a provider adapter
through the public LLM directory before opening. There is no application
factory, parallel runtime service bag or session business-forwarding layer.

## Installed distribution checks

Build the Web assets first, then build both workspace wheels from the checkout:

```bash
cd XBotv2/web && npm run build
cd ../..
uv build --all-packages --wheel --out-dir /tmp/xbot-wheels
```

Install frontend dependencies with `npm ci` if they are not present. The ignored
`XBotv2/web_dist` directory is a build input; the wheel contains its index and
assets, and `xbot web` never invokes npm. Build the wheel directly from the
checkout after generating these files. Use a clean checkout: setuptools can
retain deleted modules in a previous `build/` directory. Install
both wheels in a separate uv environment and run its `xbot` from outside the
checkout with `PYTHONPATH` unset. Check `xbot --help`, then start `xbot serve`
with a disposable data directory, query `/health` and `/sessions`, and shut it
down. Startup must not create any session records.

Inspect the actual wheel contents as well: `XBotv2/xcore.yaml`, both packages'
`py.typed` markers, runtime documentation and the bundled plugin development
skill, and the built `web_dist` index and assets must be present. Generated Web
assets remain ignored by Git but are included in the wheel. Tests, Web source,
`node_modules`, build intermediates and local runtime data must be absent. Provider interoperability and the final
post-refactor distribution require their own checks; a healthy idle server
only proves this installation and startup path.

The focused runtime acceptance command exercises real CLI processes, the
loopback OpenAI-compatible provider, streamed tool execution and history/artifact
recovery after a process restart:

```bash
.venv/bin/python -m scripts.accept_runtime --root /tmp/xbot-acceptance
# Select an independently installed wheel's console entry point:
.venv/bin/python -m scripts.accept_runtime --root /tmp/xbot-wheel-acceptance \
  --xbot /tmp/xbot-installed/bin/xbot
```

Each root must be empty. Logs and `report.json` stay there. Run `.venv/bin/python -m scripts.accept_scope_shutdown --root /tmp/xbot-sse-exit`
to terminate a real server while its SSE connection remains open and verify
that the writer claim is released.

The earlier logging
matrix and Goal evaluator probes were removed from this script; passing this
command does not assert those behaviors or replace each plugin's own tests.

## Documentation maintenance

When a public contract, route, tree entry, state owner, or security behavior
changes, update the matching page and run a link/source-reference check. Keep
the API declarations and HTTP route table derived from the current source.
The bundled `xbot-plugin-development` skill is maintained separately
but must point to these documents and use the same ownership vocabulary.

## Before a commit

Review whether the change introduced an unnecessary abstraction, optional
fallback, duplicated DTO, path join, serializer, or public export. Check that
the relevant behavior is documented, the tests exercise the public contract,
and the staged diff contains no unrelated changes.
