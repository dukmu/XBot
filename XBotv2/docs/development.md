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
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/core -q
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/integration -q
PYTHONPATH=XBotv2 .venv/bin/python scripts/check_architecture.py --scope all
PYTHONPATH=XBotv2 .venv/bin/python scripts/audit_plugin_contracts.py --format tsv
git diff --check
```

The architecture scripts are independent developer audits, not pytest tests.
They parse source with AST and do not import application modules. The contract
audit validates every explicit package-root `__all__` entry resolves, then
classifies root imports separately from declaration-module imports; it does
not claim that every exported name is desirable. Review the export list itself
when changing a public facade, and keep declarations in `contracts.py`,
`events.py`, or `protocol.py`. A green test suite does not prove interactive
Web/TUI behavior, provider interoperability, socket permissions, or
documentation accuracy.

## Documentation maintenance

When a public contract, route, tree entry, state owner, or security behavior
changes, update the matching page and run a link/source-reference check. Keep
the package-root export list and HTTP route table derived from the current
source. The bundled `xbot-plugin-development` skill is maintained separately
but must point to these documents and use the same ownership vocabulary.

## Before a commit

Review whether the change introduced an unnecessary abstraction, optional
fallback, duplicated DTO, path join, serializer, or public export. Check that
the relevant behavior is documented, the tests exercise the public contract,
and the staged diff contains no unrelated changes.
