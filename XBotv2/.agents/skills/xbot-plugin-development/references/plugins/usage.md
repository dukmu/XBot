# `usage`

Owns cumulative model usage for one thread.

See the [plugin overview](../../../../../usage/README.md) and its
[design and configuration](../../../../../usage/doc/design.md) for the
current dependencies, lifecycle, state, defaults and usage.

`api/service.py` defines `UsagePort` and `USAGE`; `api/events.py` defines `UsageUpdated`.

Cross-plugin consumers import these `api` modules. The loader entry and
business implementation are under `impl/`; the package root has no exports.
